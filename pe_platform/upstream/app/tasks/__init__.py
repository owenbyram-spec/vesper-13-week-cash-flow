# Importing a task module is what registers its tasks on the Celery app —
# the worker/beat entrypoint (app/worker.py) and celery_app.conf.imports both
# pull in this package, so EVERY module with @celery_app.task / @shared_task
# definitions that should run on the worker must be listed here. The
# structural test in tests/test_celery.py fails if a beat_schedule entry
# points at a task whose module is missing from this list.
from . import (  # noqa: F401
    accounting,
    agent,
    agent_plans,  # PLAN-EXEMPT: chunk B of ratified plan 20260714-wave3-1-agent-plans (marker RATIFICATION-2026-07-14.md) — walk + sweep task registration
    analysis,
    ar_ap_summary_refresh,  # PLAN-EXEMPT: market-ready ar-ap latency fix (Owen-ratified 2026-07-06)
    autonomy_nudge,  # PLAN-EXEMPT: ratified plan 20260713-wave1-learning-flywheel C4 (AC-9) — READY-nudge fan-out
    bank_import,  # PLAN-EXEMPT: AC-2 of ratified plan 20260716-pdf-import-async (marker RATIFICATION-2026-07-16.md) — the async statement-parse worker; ENQUEUED live from routers/bank_reconciliation.py's PDF lane, so it MUST be imported here or its jobs are silently discarded (test_every_delayed_task_is_registered enforces it)
    bank_import_reaper,
    cash_forecast_refresh,  # PLAN-EXEMPT: ratified plan 20260702-qa-remediation W-2 (F-3 CFF snapshot refresh)
    classification,
    close_watchdog,  # PLAN-EXEMPT: ratified plan 20260702-qa-remediation W-1 (stuck-close-run reaper)
    company_deletion,
    context,
    covenant_snapshot,  # PLAN-EXEMPT: AC-6/AC-7 monthly two-lens covenant snapshot task (ratified plan 20260630-covenant-redesign)
    data_deletion,
    data_export,
    digest,
    legal_ingest,  # PLAN-EXEMPT: W5-a of ratified plan 20260720-clickwrap-backend-spine — verify-on-ingest manifest mirror task; registered on the worker so the deferred ops trigger (legal.version.published webhook / safety-poll) can dispatch it
    memory,
    metric_snapshots,  # PLAN-EXEMPT: item-4 daily metric capture (ratified 2026-06-25)
    onboarding,
    proactive,
    provisioning,
    qbo_action_recovery,  # re-runs stranded APPROVED QBO write-backs (retryable / uncertain / dead running rows)
    qbo_health_check,  # PLAN-EXEMPT: QBO connection-health revocation-detection (AC-6) — proactive dead-grant sweep
    qbo_revoke_retry,
    realm_context,
    regulatory_currency,  # AC-11 monthly report-only regulatory-currency check (ratified plan 20260706-agent-path-remediation)
    reporting,
    schedules,  # PLAN-EXEMPT: AC-4 monthly schedule-generation fan-out (ratified plan 20260701-phase-a-generic-schedules)
    search_reconcile,  # PLAN-EXEMPT: Deliverable C search-index reconcile fan-out (ratified 2026-06-25)
    sync,
    test,
    webhook_recovery,
)

# Intentionally NOT imported (verified by grep over every `.delay(` /
# `.apply_async(` call site under app/ — see tests/test_celery.py's
# test_every_delayed_task_is_registered, which fails if an enqueued task's
# module is missing from the import block above):
#   bank_feed — app/services/bank_feed.py no longer exists, so importing
#   app.tasks.bank_feed raises ModuleNotFoundError. Its beat entry was removed
#   from app/celery_app.py; restore the service, this import, and the beat
#   entry together.
#   actions — app.tasks.actions.execute_approved_actions_task has NO `.delay()`
#   / `.apply_async()` caller anywhere under app/ and no beat entry, so it is
#   never enqueued; importing it would register a task nothing dispatches. Add
#   the import the moment a live caller appears (the guard test will demand it).
#   Stranded APPROVED QBO actions are re-run by qbo_action_recovery instead: it
#   selects only retryable / uncertain / dead-running executions, never every
#   APPROVED row.
#
# (onboarding, provisioning, company_deletion, data_export, data_deletion, and
# context were ALL previously in this exclusion list but are ENQUEUED — each has
# a live `.delay()` caller — so their jobs were being silently discarded by the
# worker; they are now imported above. context's caller is internal
# (refresh_all_context_caches fans out refresh_context_cache_task.delay at
# app/tasks/context.py:41), the other five are dispatched from routers/services.)

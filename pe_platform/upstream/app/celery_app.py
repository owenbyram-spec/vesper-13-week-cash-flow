import os
import logging
from copy import deepcopy

import sentry_sdk
from celery import Celery
from .config import settings
from .services.sentry_scrub import sentry_before_send, sentry_before_send_transaction
from celery.schedules import crontab

# Worker-process Sentry init. Celery worker and beat processes never import
# app.main, so without this every capture from a task was a silent no-op. Every
# Celery entrypoint imports this module (celery -A app.worker → app.worker →
# app.celery_app); the options match app/main.py exactly, and the is_active()
# guard skips a process that already initialised Sentry. Clean no-op when
# SENTRY_DSN is unset.
_SENTRY_DSN = os.getenv("SENTRY_DSN", "")
if _SENTRY_DSN and not sentry_sdk.get_client().is_active():
    sentry_sdk.init(
        dsn=_SENTRY_DSN,
        send_default_pii=False,  # explicit + review-pinned: no IP/cookie/body PII (matches app/main.py)
        include_local_variables=False,  # frame locals hold plaintext tokens; never collect them
        traces_sample_rate=0.2,
        environment=settings.environment,
        release=os.getenv("RELEASE") or os.getenv("RENDER_GIT_COMMIT"),
        before_send=sentry_before_send,  # scrub seam (DIM-026) — redact before egress
        before_send_transaction=sentry_before_send_transaction,  # transactions skip before_send
    )

celery_app = Celery(
    "qbo_agent",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    # Crash-safety pair (AP-CEL-1). Default Celery acks a task BEFORE it runs,
    # so a worker killed mid-task (OOM, SIGKILL, a Render deploy/restart) drops
    # the work silently — no retry, no error, no DLQ. task_acks_late moves the
    # ack to AFTER completion (the next worker re-runs an interrupted task) and
    # task_reject_on_worker_lost re-queues (not ack-as-failed) a task whose
    # worker died abruptly. This is AT-LEAST-ONCE delivery: it is safe here
    # because every money-mutating task is idempotent (QBOActionExecution SHA-256
    # ledger for QBO writes, status/`.first()` guards for JE + tenant-deletion +
    # Stripe-cancel, the webhook claim-ledger, upsert-by-key for sync); the
    # remaining non-money tasks (scheduled report/digest/briefing emails) carry
    # period/`last_sent_at` guards that bound a redelivery to at most one extra
    # send per period — strictly better than the silent DROP under early-ack.
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    # Don't let a worker prefetch a batch of messages a crash would strand
    # (required alongside acks_late so an interrupted task is re-queued promptly).
    worker_prefetch_multiplier=1,
    # Pin Redis's default explicitly so the v2 bank-import task's 300-second
    # lease/hard-limit remains well below broker redelivery.  Keeping 3600 does
    # not lengthen or weaken any existing job; it prevents an environment drift
    # to a shorter timeout from concurrently executing one long task twice.
    broker_transport_options={"visibility_timeout": 3600},
    result_backend_transport_options={"visibility_timeout": 3600},
    visibility_timeout=3600,
)

# Queue routing (AP-CEL-9 isolation): the nightly per-realm fan-out gets its OWN
# queue so a high-fan-out nightly run can't starve the default queue that serves
# interactive / post-sync fan-out jobs. The orchestrator + per-realm body of the
# nightly intelligence run route to "nightly"; everything else stays on the
# default queue. A worker dedicated to the "nightly" queue (or a shared worker
# consuming both) drains it independently — the two no longer contend for the
# same prefetch slots. The default queue name is pinned explicitly so the split
# is unambiguous (Celery's implicit default is also "celery"). The acks_late +
# reject_on_worker_lost delivery pair is already set in the conf.update above
# (AP-CEL-1) and is unchanged by this queue-routing block.
# CELERY-ACKS-OK: at-least-once delivery pair (task_acks_late + task_reject_on_worker_lost) already configured in the conf.update block above (lines 29-30); this block only adds queue routing, not delivery config
celery_app.conf.task_default_queue = "celery"
celery_app.conf.task_routes = {
    "app.tasks.proactive.run_nightly_intelligence": {"queue": "nightly"},
    "app.tasks.proactive.run_realm_nightly_intelligence": {"queue": "nightly"},
    # PLAN-EXEMPT: AC-4 monthly schedule generation (ratified plan 20260701-phase-a-generic-schedules).
    # The monthly schedule fan-out rides the same isolated "nightly" queue so a
    # high-fan-out 1st-of-month run can't starve the interactive default queue.
    "app.tasks.schedules.generate_monthly_schedule_entries": {"queue": "nightly"},
    "app.tasks.schedules.generate_realm_schedule_entries": {"queue": "nightly"},
}

# Task registration: app/tasks/__init__.py imports every task module, so
# importing the "app.tasks" package registers all tasks on this app. The
# previous autodiscover_tasks(["app.tasks"]) call only looked for a
# non-existent app.tasks.tasks submodule; its sole useful effect was the
# side-effect import of the app.tasks package. conf.imports makes that
# explicit and works for any entrypoint (app.worker imports app.tasks too).
celery_app.conf.imports = ("app.tasks", "app.tasks.accounting_sources")

# Celery beat schedule for periodic background jobs
BEAT_SCHEDULE_CATALOG = {
    # Resumable bank parser v2 — reclaim expired 300-second DB leases and replay
    # durable created/uncertain deliveries with their exact broker task IDs.
    # The sweep is idempotent and tenant-repins each selected row; expires below
    # the 60-second interval so a beat outage never creates a catch-up burst.
    "reap-stale-bank-import-work": {
        "task": "app.tasks.bank_import_reaper.reap_stale_bank_import_work",
        "schedule": 60.0,
        "options": {"expires": 50},
    },
    "process-pending-sync-events": {
        "task": "app.tasks.analysis.process_pending_sync_events",
        "schedule": 300.0,  # Every 5 minutes
        # expires < interval (AP-CEL-8): a backlog of stale fires after an outage
        # is REVOKED instead of draining as a catch-up burst on recovery.
        "options": {"expires": 240},
    },
    # NOTE: there is intentionally NO "sync-bank-feeds" beat entry. The proactive
    # bank-feed SYNC (app/tasks/bank_feed.py + app/services/bank_feed.py) was
    # removed in e05dd51 and superseded by webhook-driven BankFeedItem caching
    # (routers/webhooks.py writes the rows; routers/close_tasks.py reads them). The
    # dead task module + its broken service import have now been removed too — do
    # not re-add a beat entry without first restoring a real sync service.
    # PLAN-EXEMPT: AC-4 monthly schedule-entry generation beat entry (ratified plan 20260701-phase-a-generic-schedules).
    # Early on the 1st: fan out one realm-scoped child per mapped realm; each child
    # generates the JUST-CLOSED month's schedule entries + consolidated JE proposals
    # via the live engine (period locks honored + surfaced; per-schedule
    # automation_policy applied; auto_post_deterministic batches execute through the
    # existing executor ledger). Runs BEFORE check-financial-calendars (the 2nd) so
    # proposals await review before the close workflow marks the period CLOSED.
    # expires < interval (AP-CEL-8): a missed monthly fire is revoked, not stacked as
    # a catch-up burst; the unique (schedule_id, period) idempotency self-heals gaps
    # on the next manual or scheduled run.
    "monthly-schedule-entry-generation": {
        "task": "app.tasks.schedules.generate_monthly_schedule_entries",
        "schedule": crontab(day_of_month=1, hour=1, minute=0),
        "options": {"expires": 21600},  # 6h recovery window, well under the monthly interval
    },
    # Accounting module — trigger month-end workflows on the 2nd of each month
    "check-financial-calendars": {
        # The task is registered as "accounting.check_financial_calendars"
        # (explicit name= on the @shared_task decorator), NOT under its module
        # path — the entry must use the registered name or beat sends are
        # dropped as unregistered.
        "task": "accounting.check_financial_calendars",
        "schedule": crontab(day_of_month=2, hour=8, minute=0),
        # Bound a missed month-end fire to a 6h recovery window (AP-CEL-8) so an
        # outage spanning the 2nd doesn't stack a catch-up burst days later.
        "options": {"expires": 21600},
    },
    # AI-led platform — proactive intelligence engine
    "nightly-intelligence": {
        "task": "app.tasks.proactive.run_nightly_intelligence",
        "schedule": crontab(hour=2, minute=0),  # 2 AM UTC daily
        "options": {"expires": 7200},
    },
    # Optimization layer (S6) — nightly precompute of per-realm context
    # profiles + onboarding-recommendation artifacts, each strictly realm-scoped.
    "nightly-realm-context": {
        "task": "app.tasks.realm_context.build_all_realm_contexts",
        "schedule": crontab(hour=2, minute=30),  # 2:30 AM UTC daily
        "options": {"expires": 7200},
    },
    # PLAN-EXEMPT: item-4 daily metric snapshots beat entry (ratified 2026-06-25 "proceed with 1 and 2").
    # Daily point-in-time AP/AR/cash snapshots per realm. expires < interval (AP-CEL-8): a missed
    # daily fire is revoked, not stacked as a catch-up burst; the upsert is idempotent so the next
    # day's run self-heals a gap.
    "daily-metric-snapshots": {
        "task": "app.tasks.metric_snapshots.capture_all_metric_snapshots",
        "schedule": crontab(hour=3, minute=0),  # 3 AM UTC daily
        "options": {"expires": 7200},
    },
    # PLAN-EXEMPT: ratified plan 20260702-qa-remediation W-2 (F-3 #16 CFF snapshot precompute).
    # Nightly rebuild of every ACTIVE company's 13-week cash-forecast snapshot (fan-out: one
    # realm-scoped child per company that has a snapshot row), so the interactive
    # /cashflow-forecast/weekly read never needs an in-request QBO build and is always <24h
    # fresh. expires < interval (AP-CEL-8): a missed nightly fire is revoked, not stacked; the
    # per-company snapshot upsert self-heals on the next run (or on the miss-path enqueue).
    "nightly-cash-forecast-snapshots": {
        "task": "app.tasks.cash_forecast_refresh.refresh_all_forecast_snapshots",
        "schedule": crontab(hour=3, minute=30),  # 3:30 AM UTC daily, after the metric snapshots
        "options": {"expires": 7200},
    },
    # PLAN-EXEMPT: AC-6/AC-7 monthly two-lens covenant snapshot beat entry (ratified plan 20260630-covenant-redesign).
    # Snapshot every active covenant's monthly TTM + run-rate verdicts for the just-closed month + fire
    # dual two-lens alerts (run-rate early-warning + TTM breach). Runs on the 3rd at 04:00 UTC (after month-end + after the daily
    # metric snapshots at 03:00) so the prior month is fully closed. The registered name is the
    # explicit `name=` on the @celery_app.task decorator (NOT the module path) or beat sends are
    # dropped as unregistered. expires < interval (AP-CEL-8): a missed monthly fire is revoked, not
    # stacked as a catch-up burst; the per-(covenant, period) upsert self-heals the next run.
    "monthly-covenant-snapshots": {
        "task": "app.tasks.covenant_snapshot.snapshot_all_covenants",
        "schedule": crontab(day_of_month=3, hour=4, minute=0),
        "options": {"expires": 21600},  # 6h recovery window, well under the ~monthly interval
    },
    # PLAN-EXEMPT: QBO connection-health revocation-detection (AC-6) — proactive dead-grant sweep.
    # Every 2h, ping each connected realm with a cheap CompanyInfo query so a grant revoked
    # out-of-band on Intuit's side is flagged reconnect_required BEFORE a customer hits the
    # broken screen. Fan-out (AP-CEL-9): one realm-scoped child each. expires < interval
    # (AP-CEL-8): a missed fire is revoked, not stacked; the idempotent flag self-heals gaps.
    "qbo-connection-health-check": {
        "task": "app.tasks.qbo_health_check.check_all_qbo_connections",
        "schedule": crontab(minute=15, hour="*/2"),  # every 2 hours at :15
        "options": {"expires": 3600},  # 1h recovery window, well under the 2h interval
    },
    # Report scheduling — process due report schedules
    "process-report-schedules": {
        "task": "app.tasks.reporting.process_report_schedules",
        "schedule": crontab(minute=5),  # Every hour at :05
        "options": {"expires": 3600},
    },
    # Agent objectives — process due scheduled agent work
    "process-agent-objectives": {
        "task": "app.tasks.agent.process_due_objectives",
        "schedule": 300.0,  # Every 5 minutes
        "options": {"expires": 300},
    },
    # PLAN-EXEMPT: chunk B of ratified plan 20260714-wave3-1-agent-plans (marker
    # RATIFICATION-2026-07-14.md on disk). Decision 7 / AC-3: re-walk every ACTIVE
    # agent plan every 15 min (covers park re-polls between human approvals);
    # terminal plans stop matching the sweep's status filter — quiescent by
    # construction. expires < interval (AP-CEL-8): a backlog of stale fires after
    # an outage is revoked, not drained as a catch-up burst; the next tick
    # self-heals. Children are lease-guarded + fingerprint-deduped (idempotent
    # under the acks_late at-least-once pair).
    "process-active-agent-plans": {
        "task": "app.tasks.agent_plans.process_active_agent_plans",
        "schedule": 900.0,  # Every 15 minutes
        "options": {"expires": 840},
    },
    # Agent-run stale sweep — backstop for the in-band finalize: reap AgentRun
    # rows left queued/running by a crashed worker / dropped stream (incl.
    # null-org rows that block the duplicate-run guard). Plan runs consult their
    # owned lease; objective occurrences retain their own claim/recovery state.
    # The occurrence review threshold does not authorize takeover or replay.
    # expires < interval
    # (AP-CEL-8) so a recovery backlog is revoked, not drained as a burst.
    "sweep-stale-agent-runs": {
        "task": "app.tasks.agent.sweep_stale_agent_runs",
        "schedule": 300.0,  # Every 5 minutes
        "options": {"expires": 240},
    },
    # PLAN-EXEMPT: ratified plan 20260702-qa-remediation W-1 (F-1 stuck-run watchdog).
    # Close runs are excluded from the 15-min sweep above (long-running by
    # design); this hourly reaper flips a close run with no step progress for
    # >2h to failed_stale, fires a critical WatchdogAlert, and releases the
    # realm's close lease. expires < interval (AP-CEL-8): a backlog of stale
    # fires after an outage is revoked, not drained as a catch-up burst.
    "reap-stale-close-runs": {
        "task": "app.tasks.close_watchdog.reap_stale_close_runs",
        "schedule": crontab(minute=40),  # Hourly at :40
        "options": {"expires": 3000},
    },
    # Bounded retry of Intuit token revokes that failed on disconnect, so an
    # incomplete disconnect is resolved without waiting for another manual
    # disconnect call. Capped per-row at MAX_REVOKE_RETRY_ATTEMPTS.
    "retry-failed-qbo-revokes": {
        "task": "app.tasks.qbo_revoke_retry.retry_failed_qbo_revokes",
        "schedule": crontab(minute=15, hour="*/6"),  # Every 6 hours at :15
        "options": {"expires": 3600},
    },
    # QBO webhook recovery — reclaim events stuck in status="processing"
    "sweep-stale-qbo-webhook-events": {
        "task": "app.tasks.webhook_recovery.sweep_stale_qbo_webhook_events",
        "schedule": 600.0,  # Every 10 minutes
        "options": {"expires": 600},
    },
    # Stripe webhook recovery — reclaim billing events stuck in "processing"
    "sweep-stale-stripe-webhook-events": {
        "task": "app.tasks.webhook_recovery.sweep_stale_stripe_webhook_events",
        "schedule": 600.0,  # Every 10 minutes
        "options": {"expires": 600},
    },
    # PLAN-EXEMPT: Deliverable C search-index reconcile (plan 20260625-intent-routing-receivables, ratified Steven 2026-06-25).
    # Periodic backstop to the webhook path: re-index a trailing window of recent
    # invoices/bills into qbo_search_records so a missed/dropped Invoice/Bill
    # webhook still becomes findable via search_finance_records. The parent fans
    # out one short child task per realm (AP-CEL-9), and the indexer upserts by
    # (realm_id, entity_type, qbo_id) so the child is idempotent (AP-CEL-3).
    # expires < interval (AP-CEL-8): a backlog of stale fires after an outage is
    # REVOKED instead of draining as a catch-up burst; the next run self-heals.
    "reconcile-search-indexes": {
        "task": "app.tasks.search_reconcile.reconcile_all_search_indexes",
        "schedule": 21600.0,  # Every 6 hours
        "options": {"expires": 18000},
    },
    # PLAN-EXEMPT: AC-11 C10 monthly regulatory-currency check beat entry (ratified plan 20260706-agent-path-remediation; app/celery_app.py is the chunk's declared write surface).
    # REPORT-ONLY monthly scan of the in-process ASC knowledge corpus: flags
    # stale last_reviewed records, proposed-not-binding records awaiting
    # finalization re-check, and arrived amendment windows — structured log +
    # summary dict only; it NEVER edits the corpus (standards text changes only
    # through a ratified plan with CPA review). Pure scan → idempotent under the
    # acks_late at-least-once pair. Runs on the 1st at 06:00 UTC, clear of the
    # 01:00 schedule generation and the 02:00-04:00 nightly lanes. expires <
    # interval (AP-CEL-8): a missed monthly fire is revoked, not stacked; the
    # next month's scan (or an ad-hoc enqueue) self-heals the gap.
    "monthly-regulatory-currency-check": {
        "task": "app.tasks.regulatory_currency.regulatory_currency_check",
        "schedule": crontab(day_of_month=1, hour=6, minute=0),
        "options": {"expires": 21600},  # 6h recovery window, well under the monthly interval
    },
    # Weekly Review digest email — Monday 08:00 UTC (task in app/tasks/digest.py)
    "weekly-digest-monday-8am": {
        "task": "tasks.send_weekly_digest_all",
        "schedule": crontab(hour=8, minute=0, day_of_week=1),
        "options": {"expires": 21600},
    },
    # ── plan 20260713-wave1-learning-flywheel — orchestrator-merged beat entries (decision
    # 10: agents RETURNED these as text; the orchestrator owns this shared file). ──────────
    # PLAN-EXEMPT: ratified plan 20260713-wave1-learning-flywheel (marker
    # RATIFICATION-2026-07-13.md) — C1/C2/C4 beat wiring, merged centrally per decision 10.
    # C1 (AC-1/AC-3): daily categorization sweep — per-realm rules-first/LLM-fallback
    # predictions land executable PENDING RECLASSIFY_TXN proposals (pinned payload;
    # execution-time SyncToken); L3 auto-exec mirrors the actions.py recipe, shadow default
    # = 100% human. Idempotent under acks_late at-least-once redelivery: one batched
    # inputs_fingerprint IN dedup query before any insert, so a re-run on identical state
    # drafts zero new rows. expires < interval (AP-CEL-8): a missed daily fire is revoked,
    # not stacked; the next day's sweep self-heals the gap.
    "daily-categorization-sweep": {
        "task": "app.tasks.classification.classify_pending_transactions_task",
        "schedule": crontab(hour=4, minute=45),  # clear of the 02:00–04:00 nightly lanes
        "options": {"expires": 21600},  # 6h recovery window, well under the daily interval
    },
    # C2 (AC-6): memory maintenance fan-outs (AP-CEL-9 — parent enumerates mapped realms,
    # one realm-scoped child each). Children are idempotent: the backfill selects only
    # embedding_vector-IS-NULL rows; consolidation stamps superseded_by_id / expires_at
    # only where unset. expires < interval (AP-CEL-8).
    "reembed-company-knowledge-nightly": {
        "task": "app.tasks.memory.reembed_all_company_knowledge",
        "schedule": crontab(hour=3, minute=40),
        "options": {"expires": 21600},  # 6h recovery window, well under the daily interval
    },
    # After the backfill so same-night embedded rows are consolidatable.
    "consolidate-company-knowledge-nightly": {
        "task": "app.tasks.memory.consolidate_all_company_knowledge",
        "schedule": crontab(hour=4, minute=20),
        "options": {"expires": 21600},  # 6h recovery window, well under the daily interval
    },
    # PLAN-EXEMPT: chunk 1 of ratified plan 20260714-wave3-3-episodic-toolfail-memory
    # (marker RATIFICATION-2026-07-14.md). LG-4 episodic-memory distillation fan-out
    # (AP-CEL-9): one realm-scoped child per mapped realm, each distilling terminal-
    # success AgentRuns into agent_episodes (NOT-IN + UNIQUE(agent_run_id) idempotent —
    # safe under the acks_late at-least-once pair). agent_episodes is a SEPARATE table
    # (no CompanyKnowledge consolidation), so the slot is order-independent; 03:20 is
    # clear of the 03:00 metric snapshots and ahead of the 03:40 reembed lane. expires
    # < interval (AP-CEL-8): a missed nightly fire is revoked, not stacked as a burst.
    "distill-agent-episodes-nightly": {
        "task": "app.tasks.memory.distill_all_agent_episodes",
        "schedule": crontab(hour=3, minute=20),
        "options": {"expires": 21600},  # 6h recovery window, well under the daily interval
    },
    # C4 (AC-9): daily once-per-transition autonomy READY nudge. Fan-out (AP-CEL-9): one
    # org-scoped child per active tenant. Idempotent: the child's verdict-compare existence
    # check makes re-delivery a no-op. expires < interval (AP-CEL-8): a missed daily fire
    # is revoked, not stacked.
    "autonomy-ready-nudge": {
        "task": "app.tasks.autonomy_nudge.check_all_autonomy_gates",
        "schedule": crontab(hour=13, minute=15),  # daily, after the nightly correction-capture lanes
        "options": {"expires": 21600},  # 6h recovery window, well under the daily interval
    },
}



def select_beat_schedule(names: str) -> dict:
    """Select exact catalog names, retaining catalog order and independent entries."""
    selected = {name.strip() for name in names.split(",") if name.strip()}
    if selected - BEAT_SCHEDULE_CATALOG.keys():
        raise ValueError("CELERY_BEAT_SCHEDULE_ALLOWLIST contains an invalid entry")
    return {name: deepcopy(entry) for name, entry in BEAT_SCHEDULE_CATALOG.items() if name in selected}


celery_app.conf.beat_schedule = select_beat_schedule(settings.celery_beat_schedule_allowlist)
logging.getLogger(__name__).info("Selected periodic schedules: %s", ",".join(celery_app.conf.beat_schedule) or "none")

"""Monthly REPORT-ONLY regulatory-currency check (AC-11, plan 20260706-agent-path-remediation C10).

Scans the in-process ASC knowledge corpus (``app.assistant.asc_knowledge.data.ASC_TOPICS``
— a pure in-memory read; no DB session is opened) and flags to the platform owner:

  (a) records whose ``last_reviewed`` is older than ``REGULATORY_REVIEW_MAX_AGE_DAYS``
      (env-overridable, default 180 — the close_watchdog CLOSE_RUN_STALE_SECONDS
      pattern; read at CALL time so a malformed value can never crash-loop an
      import, per the config _env_int lesson),
  (b) records with ``binding_status == "proposed-not-binding"`` (the
      re-check-for-finalization list — a proposal must be re-examined for a
      final ASU, and must never silently age into "assumed binding"),
  (c) records with a non-empty ``amended_by`` whose ``effective_date`` text
      parses to a calendar date that has arrived (simple date comparison);
      where the date is null/unparseable the record is instead flagged
      "ungrounded — pending CPA pass" (never guessed).

REPORT CHANNEL — Sentry structured event + log line (SUPERSESSION, ratified plan
20260713-wave0-ai-integration-p0, decision 1). The original decision here was
structured-log-ONLY: the survey of owner channels found no clean fit —
``WatchdogAlert`` is ``company_id NOT NULL`` (per-tenant financial alerts; a
platform-wide corpus check has no tenant to key on), and
``send_debug_incident_notification`` is bound to the DebugIncident/subscription
pipeline (routers/webhooks.py). That recorded blocker is now solved with
already-wired infrastructure instead of a new subsystem: when anything is
flagged the task ADDITIONALLY emits ONE ``sentry_sdk.capture_message`` at
WARNING with counts + flagged corpus ids as structured context (ints/ids only —
redaction-safe; nothing is emitted on a clean run), reaching the platform owner
through the Sentry channel the app already operates. The worker-process SDK is
initialized in ``app/celery_app.py`` (previously only ``app/main.py`` — without
worker init the emission would be a silent no-op, refuter change E3). The
structured WARNING/INFO log line and the JSON-serializable summary dict are
retained unchanged.

INVARIANTS: this task performs ZERO writes — no file I/O, no DB session, no
mutation of the corpus objects (asserted in tests/test_regulatory_currency_task.py).
Standards text changes only through a ratified plan with CPA review, and
fasb.org is never fetched (it 403-blocks bots; the design is date-based
flagging, zero network). Idempotent by construction (pure scan -> report), so
it is safe under the app-wide acks_late at-least-once delivery.
"""
# PLAN-EXEMPT: ratified plan 20260713-wave0-ai-integration-p0 C3 (RK-4, decision 1) — marker
# RATIFICATION-2026-07-13.md verified on disk; active-plan.json absent in this dispatched
# sub-agent session (the ratifying keyboard turn lives in the orchestrator transcript).
from __future__ import annotations

import logging
import os
import re
from datetime import date, datetime, timezone
from typing import TypedDict

# PLAN-EXEMPT: ratified plan 20260713-wave0-ai-integration-p0 C3 (RK-4, decision 1) — the
# owner-report Sentry channel. capture_message is a clean no-op when the SDK is uninitialized
# (NonRecordingClient), so the log-only behavior is preserved wherever no DSN is configured.
import sentry_sdk

from ..assistant.asc_knowledge.data import ASC_TOPICS
from ..celery_app import celery_app

logger = logging.getLogger(__name__)

DEFAULT_REVIEW_MAX_AGE_DAYS = 180

UNGROUNDED_REASON = "ungrounded — pending CPA pass"

# Matches the corpus's effective-date prose ("Dec 15, 2025" / "December 15, 2027").
_EFFECTIVE_DATE_RE = re.compile(
    r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+(\d{1,2}),?\s+(\d{4})\b"
)
_MONTH_NUM = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}


class CurrencyCounts(TypedDict):
    """Per-category flag counts for one scan."""

    stale_review: int
    proposed_not_binding: int
    amendment_window_arrived: int
    ungrounded_pending_cpa: int


class CurrencySummary(TypedDict):
    """The JSON-serializable report the task returns (and logs)."""

    status: str
    as_of: str
    review_max_age_days: int
    checked: int
    counts: CurrencyCounts
    stale_review_ids: list[int]
    proposed_not_binding_ids: list[int]
    amendment_window_arrived_ids: list[int]
    ungrounded_pending_cpa_ids: list[int]


def _today() -> date:
    """Clock seam — tests pin this to a fixed date (testing.md §8.2)."""
    return datetime.now(timezone.utc).date()


def _review_max_age_days() -> int:
    """Env-overridable staleness threshold, parsed safely at call time."""
    raw = os.getenv("REGULATORY_REVIEW_MAX_AGE_DAYS")
    if raw is None or not raw.strip():
        return DEFAULT_REVIEW_MAX_AGE_DAYS
    try:
        return int(raw.strip())
    except ValueError:
        logger.warning(
            "REGULATORY_REVIEW_MAX_AGE_DAYS=%r is not an integer; using default %d",
            raw,
            DEFAULT_REVIEW_MAX_AGE_DAYS,
        )
        return DEFAULT_REVIEW_MAX_AGE_DAYS


def _field(record: dict, key: str) -> object:
    """Read a corpus field without mutating the record (no setdefault/assign)."""
    try:
        return record[key]
    except KeyError:
        return None


def _parse_iso_date(value: object) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        return None


def _parse_effective_dates(text: object) -> list[date]:
    """Extract every calendar date from an effective-date sentence; [] if none parse."""
    if not isinstance(text, str):
        return []
    dates: list[date] = []
    for month, day, year in _EFFECTIVE_DATE_RE.findall(text):
        try:
            dates.append(date(int(year), _MONTH_NUM[month], int(day)))
        except ValueError:
            continue  # e.g. "Feb 30" — skip the malformed match, keep scanning
    return dates


# PLAN-EXEMPT: ratified plan 20260713-wave0-ai-integration-p0 C3 (RK-4/AC-7, decision 1) —
# marker RATIFICATION-2026-07-13.md verified on disk; active-plan.json absent in this
# dispatched sub-agent session.
def _emit_owner_report(summary: CurrencySummary, total_flagged: int) -> None:
    """Route the flagged-run report to the platform owner via Sentry.

    Emits exactly ONE ``capture_message`` at WARNING with counts + flagged corpus
    record ids as structured context. Every context value is an int or a list of
    ints — no tenant, financial, or free-text payload — so the event is
    redaction-safe by construction. A clean run emits nothing (the caller gates
    on ``total_flagged``); an uninitialized SDK (no DSN) makes this a no-op.
    """
    with sentry_sdk.new_scope() as scope:
        scope.set_context(
            "regulatory_currency",
            {  # OBS-OK: counts + corpus record ids only (ints) — no tenant or financial payload
                "checked": summary["checked"],
                "review_max_age_days": summary["review_max_age_days"],
                "stale_review": summary["counts"]["stale_review"],
                "proposed_not_binding": summary["counts"]["proposed_not_binding"],
                "amendment_window_arrived": summary["counts"]["amendment_window_arrived"],
                "ungrounded_pending_cpa": summary["counts"]["ungrounded_pending_cpa"],
                "stale_review_ids": summary["stale_review_ids"],
                "proposed_not_binding_ids": summary["proposed_not_binding_ids"],
                "amendment_window_arrived_ids": summary["amendment_window_arrived_ids"],
                "ungrounded_pending_cpa_ids": summary["ungrounded_pending_cpa_ids"],
            },
        )
        sentry_sdk.capture_message(
            f"regulatory_currency_check flagged {total_flagged} ASC corpus records",
            level="warning",
        )


@celery_app.task(name="app.tasks.regulatory_currency.regulatory_currency_check")
def regulatory_currency_check() -> CurrencySummary:
    """Report-only scan of the ASC corpus for regulatory-currency drift."""
    today = _today()
    max_age_days = _review_max_age_days()

    stale_review_ids: list[int] = []
    proposed_not_binding_ids: list[int] = []
    amendment_window_arrived_ids: list[int] = []
    ungrounded_pending_cpa_ids: list[int] = []

    for record in ASC_TOPICS:
        topic_id = record["id"]

        # (a) staleness: last_reviewed older than the threshold. An unparseable
        # or missing last_reviewed is itself an ungrounded record.
        last_reviewed = _parse_iso_date(_field(record, "last_reviewed"))
        if last_reviewed is None:
            ungrounded_pending_cpa_ids.append(topic_id)
        elif (today - last_reviewed).days > max_age_days:
            stale_review_ids.append(topic_id)

        # (b) a proposal is ALWAYS on the re-check-for-finalization list.
        if _field(record, "binding_status") == "proposed-not-binding":
            proposed_not_binding_ids.append(topic_id)

        # (c) amendment tracking: only records that name an amending ASU.
        if _field(record, "amended_by"):
            effective_dates = _parse_effective_dates(_field(record, "effective_date"))
            if not effective_dates:
                ungrounded_pending_cpa_ids.append(topic_id)
            elif min(effective_dates) <= today:
                amendment_window_arrived_ids.append(topic_id)

    counts = CurrencyCounts(
        stale_review=len(stale_review_ids),
        proposed_not_binding=len(proposed_not_binding_ids),
        amendment_window_arrived=len(amendment_window_arrived_ids),
        ungrounded_pending_cpa=len(ungrounded_pending_cpa_ids),
    )
    summary = CurrencySummary(
        status="ok",
        as_of=today.isoformat(),
        review_max_age_days=max_age_days,
        checked=len(ASC_TOPICS),
        counts=counts,
        stale_review_ids=stale_review_ids,
        proposed_not_binding_ids=proposed_not_binding_ids,
        amendment_window_arrived_ids=amendment_window_arrived_ids,
        ungrounded_pending_cpa_ids=ungrounded_pending_cpa_ids,
    )

    total_flagged = sum(counts.values())
    # PLAN-EXEMPT: ratified plan 20260713-wave0-ai-integration-p0 C3 (RK-4/AC-7, decision 1) —
    # ONE Sentry WARNING per flagged run (nothing when clean), counts + corpus record ids only.
    if total_flagged:
        _emit_owner_report(summary, total_flagged)
    emit = logger.warning if total_flagged else logger.info
    emit(
        "regulatory_currency_check: checked=%d stale_review=%d (%s) "
        "proposed_not_binding=%d (%s) amendment_window_arrived=%d (%s) "
        "ungrounded_pending_cpa=%d (%s) [%s; threshold=%dd; report-only — "
        "corpus changes require a ratified plan + CPA review]",
        summary["checked"],
        counts["stale_review"],
        stale_review_ids,
        counts["proposed_not_binding"],
        proposed_not_binding_ids,
        counts["amendment_window_arrived"],
        amendment_window_arrived_ids,
        counts["ungrounded_pending_cpa"],
        ungrounded_pending_cpa_ids,
        UNGROUNDED_REASON,
        max_age_days,
    )
    return summary

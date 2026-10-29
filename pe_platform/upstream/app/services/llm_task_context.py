# PLAN-EXEMPT: ratified plan 20260702-qa-remediation W-1
"""LLM task context + the per-close-run token budget (F-1 / F-7, wave W-1).

Two concerns, one seam:

1. **Attribution** — every LLM call in the platform records a ledger row
   (``LLMUsageEvent``); this module carries the *work-unit* dimension the
   recorders stamp on that row: ``task_type`` (chat | close | briefing |
   watchdog | schedule | board_report | other), the owning ``run_id``
   (``agent_runs.id``) and a human-readable ``step`` label. Callers that know
   their task type pass it explicitly; run-scoped background drivers (the
   close-orchestration walk, the nightly engine, briefing generation) bind it
   ONCE here on a ``ContextVar`` so every LLM call made underneath — lane
   calls, review-check drafts, tool handlers — inherits it without threading
   arguments through the whole graph. Mirrors ``tenant_tag.py``: default
   ``None``, never inherit a stale ambient context.

2. **The close budget (FAIL-LOUD)** — when the bound context says
   ``task_type == "close"`` with a ``run_id``, ``enforce_close_run_budget``
   sums the run's ledger rows and raises :class:`CloseBudgetExceededError`
   BEFORE the next provider call once the per-run token budget
   (``settings.close_run_token_budget``, per-realm override
   ``ReviewPolicy.close_run_token_budget``) is spent. The wrapper seams
   (``app/services/llm.py`` ``predict_text_result`` and the agent loop's
   ``_create_message``) call it pre-call, OUTSIDE their provider try/except,
   so the error can never be laundered into a fallback or a degraded
   continue. Metering itself stays FAIL-OPEN (a ledger-write error must never
   break the feature); only a *genuine over-budget* raises — an infrastructure
   error reading the budget logs loudly and allows.
"""
from __future__ import annotations

import contextlib
import logging
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Iterator, Optional
from uuid import UUID

logger = logging.getLogger(__name__)

# The closed vocabulary for the ledger's task_type dimension. "other" is the
# honest default for work that has not been classified yet — never invent a
# new value outside this set (the admin rollup groups on it).
LLM_TASK_TYPES = frozenset(
    # "classification" added by plan 20260716-classification-hardening AC-2 —
    # the transaction-classification lane (sync + nightly sweep) was invisible
    # in llm_usage_events (batch-5 evidence: no classification task_type
    # existed anywhere while the lane billed daily).
    {
        "chat",
        "close",
        "briefing",
        "watchdog",
        "schedule",
        "board_report",
        "classification",
        "bank_statement_parse",
        "other",
    }
)
DEFAULT_TASK_TYPE = "other"


@dataclass
class ProviderInvocationTrace:
    """In-process cross-check for physical calls versus durable envelopes.

    This is never evidence by itself. The worker persists only the counts, then
    accepts a run for evidence when they reconcile exactly to committed provider
    attempts and committed usage rows.
    """

    calls_started: int = 0
    envelope_failures: int = 0
    provider_attempt_ids: list[UUID] = field(default_factory=list)

    def observe_call(self, provider_attempt_id: UUID | None) -> None:
        self.calls_started += 1
        if provider_attempt_id is None:
            self.envelope_failures += 1
        else:
            self.provider_attempt_ids.append(provider_attempt_id)

    def observe_envelope_failure(self) -> None:
        """Record a pre-call refusal without miscounting it as physical spend."""
        self.envelope_failures += 1


@dataclass(frozen=True)
class LLMTaskContext:
    """The work-unit identity for LLM calls made in the current context."""

    task_type: str
    run_id: Optional[int] = None  # agent_runs.id when the work runs inside a tracked run
    step: Optional[str] = None  # human-readable step label (e.g. "REVIEW_ANOMALIES:123")
    realm_id: Optional[str] = None  # for per-realm budget policy resolution
    bank_statement_import_id: int | None = None
    bank_statement_delivery_attempt_id: UUID | None = None
    bank_statement_chunk_id: UUID | None = None
    provider_trace: ProviderInvocationTrace | None = None


# Default None: never inherit an ambient work unit (same invariant as tenant_tag).
_current: ContextVar[Optional[LLMTaskContext]] = ContextVar("current_llm_task", default=None)


def normalize_task_type(value: Optional[str]) -> str:
    """Clamp a task_type to the closed vocabulary (unknown → "other")."""
    candidate = (value or "").strip().lower()
    return candidate if candidate in LLM_TASK_TYPES else DEFAULT_TASK_TYPE


@contextlib.contextmanager
def bind_llm_task_context(
    task_type: str,
    *,
    run_id: Optional[int] = None,
    step: Optional[str] = None,
    realm_id: Optional[str] = None,
    bank_statement_import_id: int | None = None,
    bank_statement_delivery_attempt_id: UUID | None = None,
    bank_statement_chunk_id: UUID | None = None,
    provider_trace: ProviderInvocationTrace | None = None,
) -> Iterator[LLMTaskContext]:
    """Bind the LLM task context for the current execution context (token reset)."""
    ctx = LLMTaskContext(
        task_type=normalize_task_type(task_type),
        run_id=run_id,
        step=step,
        realm_id=realm_id,
        bank_statement_import_id=bank_statement_import_id,
        bank_statement_delivery_attempt_id=bank_statement_delivery_attempt_id,
        bank_statement_chunk_id=bank_statement_chunk_id,
        provider_trace=provider_trace,
    )
    token = _current.set(ctx)
    try:
        yield ctx
    finally:
        _current.reset(token)


def current_llm_task_context() -> Optional[LLMTaskContext]:
    """Return the bound LLM task context, or None when nothing is bound."""
    return _current.get()


class CloseBudgetExceededError(RuntimeError):
    """A close run has spent its LLM token budget — the run must FAIL LOUDLY.

    Raised by the wrapper BEFORE the provider call that would exceed the
    budget, so the over-budget call itself is never made. Callers in the close
    walk must let this propagate (never degrade it into a
    'continue-with-deterministic-message' path) — the walk converts it into a
    failed run + WatchdogAlert + finding row.
    """

    def __init__(
        self,
        *,
        run_id: int,
        spent_tokens: int,
        budget_tokens: int,
        step: Optional[str] = None,
    ) -> None:
        self.run_id = run_id
        self.spent_tokens = spent_tokens
        self.budget_tokens = budget_tokens
        self.step = step
        super().__init__(
            f"Close run {run_id} exceeded its LLM token budget: "
            f"spent {spent_tokens} >= budget {budget_tokens}"
            + (f" (at step {step})" if step else "")
        )


def close_run_tokens_spent(run_id: int, db=None) -> int:
    """Total ledger tokens (input+output) already recorded for one agent run.

    Sums ``LLMUsageEvent.total_tokens`` for ``agent_run_id == run_id``. Opens a
    short-lived ``SessionLocal`` (closed in finally) unless the caller passes a
    session. The read runs under the audited server-side RLS bypass exactly
    like the recorder's write path (llm_usage.py): it is a trusted, run-keyed
    scalar aggregate, not a tenant-facing read.
    """
    from sqlalchemy import func as sa_func

    from ..models import LLMUsageEvent

    def _sum(session) -> int:
        value = (
            session.query(sa_func.coalesce(sa_func.sum(LLMUsageEvent.total_tokens), 0))
            .filter(LLMUsageEvent.agent_run_id == run_id)
            .scalar()
        )
        return int(value or 0)

    if db is not None:
        return _sum(db)

    from ..db import SessionLocal
    from ..tenant_rls import tenant_scope_live

    session = SessionLocal()
    try:
        # CROSS-TENANT-OK: trusted server-side, run-keyed scalar aggregate for
        # budget enforcement (mirrors the recorder's audited bypass write).
        with tenant_scope_live(session, bypass=True):
            return _sum(session)
    finally:
        session.close()


def effective_close_run_token_budget(realm_id: Optional[str] = None, db=None) -> int:
    """The per-close-run token budget: per-realm ReviewPolicy override → default.

    A positive ``ReviewPolicy.close_run_token_budget`` on the realm's active
    policy wins; otherwise ``settings.close_run_token_budget``. Fail-soft on
    any policy-read error (log + default) — a policy lookup must never crash a
    close run; the budget itself still applies.
    """
    from ..config import settings

    default_budget = int(settings.close_run_token_budget)
    if not realm_id:
        return default_budget

    def _resolve(session) -> int:
        from ..models import ReviewPolicy

        policy = (
            session.query(ReviewPolicy)
            .filter(
                ReviewPolicy.qbo_realm_id == realm_id,
                ReviewPolicy.is_active == True,  # noqa: E712 — SQLAlchemy column truth-test
            )
            .first()
        )
        override = getattr(policy, "close_run_token_budget", None) if policy else None
        if override is not None and int(override) > 0:
            return int(override)
        return default_budget

    try:
        if db is not None:
            return _resolve(db)
        from ..db import SessionLocal

        session = SessionLocal()
        try:
            return _resolve(session)
        finally:
            session.close()
    except Exception as exc:  # fail-soft: policy read must not crash the run
        logger.warning(
            "[llm_task_context] close budget policy read failed for realm=%s; "
            "using platform default %s: %s",
            realm_id,
            default_budget,
            exc,
        )
        return default_budget


def enforce_close_run_budget() -> None:
    """Raise :class:`CloseBudgetExceededError` when the bound close run is over budget.

    No-op unless the bound context is ``task_type == "close"`` with a
    ``run_id``. Called by the LLM wrapper seams BEFORE each provider call.
    FAIL-LOUD on a genuine over-budget; FAIL-OPEN (loud log) on an
    infrastructure error reading the spend — a DB blip must not kill a close,
    but an exhausted budget must never be spent past.
    """
    ctx = current_llm_task_context()
    if ctx is None or ctx.task_type != "close" or not ctx.run_id:
        return
    try:
        budget = effective_close_run_token_budget(realm_id=ctx.realm_id)
        spent = close_run_tokens_spent(ctx.run_id)
    except Exception as exc:  # infra error → allow, but LOUDLY
        logger.error(
            "[llm_task_context] close budget check failed for run=%s (allowing this call): %s",
            ctx.run_id,
            exc,
        )
        return
    if spent >= budget:
        raise CloseBudgetExceededError(
            run_id=ctx.run_id,
            spent_tokens=spent,
            budget_tokens=budget,
            step=ctx.step,
        )

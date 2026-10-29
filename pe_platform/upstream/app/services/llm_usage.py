"""
Best-effort recorder for LLMUsageEvent rows (audit item S4).

Every LLM call should produce exactly one LLMUsageEvent. Ordinary telemetry is
best-effort and never breaks the feature. The resumable bank parser is the narrow
exception: its pre-call attempt + ledger envelope is the spend authorization, so
failure there refuses the SDK call before cost is incurred.

Nothing in here stores raw prompts, raw financial data, secrets, or tokens —
only counts, costs, latency, classified error type, and a redacted error
string.
"""
from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from uuid import UUID

from ..db import SessionLocal
from ..models import (
    BankStatementParseChunk,
    BankStatementParseDeliveryAttempt,
    BankStatementParseProviderAttempt,
    LLMUsageEvent,
)

logger = logging.getLogger(__name__)


# ── Error taxonomy ──────────────────────────────────────────────────────────
# Stable, low-cardinality classification used for routing/alerting.
ERROR_RATE_LIMIT = "rate_limit"
ERROR_QUOTA_EXHAUSTED = "quota_exhausted"
ERROR_AUTH = "auth"
ERROR_TIMEOUT = "timeout"
ERROR_PARSE = "parse"
ERROR_OTHER = "other"

_CACHE_TOKEN_KEYS = (
    "cached_input_tokens",
    "cached_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)


def classify_error(exc: BaseException) -> str:
    """Map a provider exception (or generic error) to the error taxonomy.

    Handles openai.RateLimitError / APIStatusError(429),
    anthropic.RateLimitError, and gemini ResourceExhausted without importing
    the SDKs eagerly (they may not all be installed in every environment).
    """
    name = type(exc).__name__
    module = type(exc).__module__ or ""
    text = str(exc) or ""
    lowered = text.lower()

    status_code = (
        getattr(exc, "status_code", None)
        or getattr(exc, "code", None)
        or getattr(getattr(exc, "response", None), "status_code", None)
    )

    # Rate limiting (429 / explicit RateLimitError types).
    if "ratelimit" in name.lower() or status_code == 429:
        return ERROR_RATE_LIMIT
    if "toomanyrequests" in name.lower():
        return ERROR_RATE_LIMIT

    # Quota / resource exhaustion (Gemini ResourceExhausted, billing quota).
    if "resourceexhausted" in name.lower():
        return ERROR_QUOTA_EXHAUSTED
    if "quota" in lowered or "insufficient_quota" in lowered or "billing" in lowered:
        return ERROR_QUOTA_EXHAUSTED

    # Auth / permission.
    if status_code in (401, 403):
        return ERROR_AUTH
    if any(token in name.lower() for token in ("authentication", "permissiondenied", "unauthenticated")):
        return ERROR_AUTH
    if "api key" in lowered or "api_key" in lowered or "unauthorized" in lowered:
        return ERROR_AUTH

    # Timeouts.
    if "timeout" in name.lower() or isinstance(exc, TimeoutError):
        return ERROR_TIMEOUT
    if "timed out" in lowered or "deadline" in lowered:
        return ERROR_TIMEOUT

    # Parse / decode failures.
    if name in ("JSONDecodeError", "ValidationError") or "jsondecode" in module.lower():
        return ERROR_PARSE
    if "parse" in lowered and "json" in lowered:
        return ERROR_PARSE

    return ERROR_OTHER


# ── Redaction ─────────────────────────────────────────────────────────────
# Scrub anything that looks like a secret/token/key out of error text before
# we persist it. We never store raw financial data in error strings, but keys
# can leak into provider error messages, so we are aggressive.
_REDACTION_PATTERNS = [
    # OpenAI-style keys (sk-..., sk-proj-...), Anthropic (sk-ant-...).
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"),
    # Google API keys (AIza...).
    re.compile(r"AIza[0-9A-Za-z_\-]{20,}"),
    # Bearer tokens.
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{8,}"),
    # Generic "key"/"token"/"secret" = value assignments.
    re.compile(r"(?i)(api[_-]?key|access[_-]?token|refresh[_-]?token|secret|password)\s*[=:]\s*\S+"),
    # Long opaque token-ish blobs (JWT segments etc.).
    re.compile(r"\b[A-Za-z0-9_\-]{40,}\b"),
]

_REDACTED = "[REDACTED]"
_MAX_ERROR_CHARS = 2000


def redact_error(raw: Optional[str]) -> Optional[str]:
    """Scrub secrets/keys from error text and bound its length."""
    if not raw:
        return None
    text = str(raw)
    for pattern in _REDACTION_PATTERNS:
        text = pattern.sub(_REDACTED, text)
    if len(text) > _MAX_ERROR_CHARS:
        text = text[:_MAX_ERROR_CHARS] + "...[truncated]"
    return text


# ── Cache / token extraction ─────────────────────────────────────────────
def _coerce_int(value: Any) -> Optional[int]:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def extract_token_fields(token_usage: Optional[Dict[str, Any]]) -> Dict[str, Optional[int]]:
    """Normalize provider token/cache usage into LLMUsageEvent columns.

    Recognizes:
      - input_tokens / output_tokens (all providers, normalized upstream)
      - cache_creation_input_tokens / cache_read_input_tokens (Anthropic)
      - cached_input_tokens / cached_tokens (OpenAI prompt caching)
    """
    usage = token_usage or {}
    input_tokens = _coerce_int(usage.get("input_tokens"))
    output_tokens = _coerce_int(usage.get("output_tokens"))
    cache_creation = _coerce_int(usage.get("cache_creation_input_tokens"))
    cache_read = _coerce_int(usage.get("cache_read_input_tokens"))
    cached_input = _coerce_int(
        usage.get("cached_input_tokens")
        if usage.get("cached_input_tokens") is not None
        else usage.get("cached_tokens")
    )

    total = usage.get("total_tokens")
    total_tokens = _coerce_int(total)
    if total_tokens is None and (input_tokens is not None or output_tokens is not None):
        total_tokens = (input_tokens or 0) + (output_tokens or 0)

    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_creation_input_tokens": cache_creation,
        "cache_read_input_tokens": cache_read,
        "cached_input_tokens": cached_input,
        "total_tokens": total_tokens,
    }


class BankStatementProviderEnvelopeError(RuntimeError):
    """A bank-parser paid call was refused because its durable envelope failed."""


def _bank_statement_provider_envelope_is_committed(
    *,
    attempt_id: UUID,
    import_id: int,
    delivery_attempt_id: UUID,
    chunk_id: UUID,
    realm_id: str,
    provider: str,
    model: str,
    lane: str,
) -> bool:
    """Resolve an ambiguous commit outcome from a fresh, read-only session."""
    session = None
    try:
        session = SessionLocal()
        from ..tenant_rls import tenant_scope_live

        with tenant_scope_live(session, bypass=True):
            attempt = (
                session.query(BankStatementParseProviderAttempt)
                .filter(BankStatementParseProviderAttempt.id == attempt_id)
                .one_or_none()
            )
            if attempt is None or attempt.usage_event_id is None:
                return False
            delivery = (
                session.query(BankStatementParseDeliveryAttempt)
                .filter(
                    BankStatementParseDeliveryAttempt.id == delivery_attempt_id,
                    BankStatementParseDeliveryAttempt.bank_statement_import_id
                    == import_id,
                    BankStatementParseDeliveryAttempt.realm_id == realm_id,
                )
                .one_or_none()
            )
            chunk = (
                session.query(BankStatementParseChunk)
                .filter(
                    BankStatementParseChunk.id == chunk_id,
                    BankStatementParseChunk.bank_statement_import_id == import_id,
                    BankStatementParseChunk.realm_id == realm_id,
                )
                .one_or_none()
            )
            event = (
                session.query(LLMUsageEvent)
                .filter(LLMUsageEvent.id == attempt.usage_event_id)
                .one_or_none()
            )
            return bool(
                delivery is not None
                and chunk is not None
                and event is not None
                and delivery.chunk_id == chunk.id == attempt.chunk_id
                and delivery.lifecycle_status == "started"
                and chunk.lifecycle_status == "leased"
                and attempt.bank_statement_import_id == import_id
                and attempt.delivery_attempt_id == delivery.id
                and attempt.realm_id == realm_id
                and attempt.organization_id
                == delivery.organization_id
                == chunk.organization_id
                and attempt.organization_company_id
                == delivery.organization_company_id
                == chunk.organization_company_id
                and attempt.attempt_ordinal
                == int(chunk.application_retry_count or 0)
                and attempt.provider_call_id == attempt.id.hex
                and attempt.lifecycle_status == "started"
                and attempt.status == "started"
                and attempt.tenant_tag_succeeded is True
                and attempt.completed_at is None
                and attempt.provider == event.provider == provider
                and attempt.model == event.model == model
                and attempt.lane == event.route == lane
                and event.task_type == "bank_statement_parse"
                and event.status == "started"
                and event.organization_id == attempt.organization_id
                and event.organization_company_id
                == attempt.organization_company_id
                and event.qbo_realm_id == realm_id
            )
    except Exception as exc:
        logger.error(
            "[BankParseTelemetry] provider envelope reconciliation failed: %s",
            type(exc).__name__,
        )
        return False
    finally:
        if session is not None:
            session.close()


def begin_bank_statement_provider_attempt(
    *,
    provider: str,
    model: str,
    lane: str,
) -> UUID | None:
    """Commit the physical-call envelope before a bank-parser SDK invocation.

    This helper is intentionally a no-op outside the bound bank parser context.
    A bound bank-parser failure is fail-closed before provider spend: without the
    committed attempt and ledger placeholder, no SDK invocation is authorized.
    """
    from .llm_task_context import current_llm_task_context
    from .tenant_tag import current_tenant_context

    task_ctx = current_llm_task_context()
    if task_ctx is None or task_ctx.task_type != "bank_statement_parse":
        return None

    trace = task_ctx.provider_trace
    attempt_id: UUID | None = None
    candidate_attempt_id: UUID | None = None
    import_id = task_ctx.bank_statement_import_id
    delivery_attempt_id = task_ctx.bank_statement_delivery_attempt_id
    chunk_id = task_ctx.bank_statement_chunk_id
    realm_id = str(task_ctx.realm_id or "").strip()
    try:
        if import_id is None or delivery_attempt_id is None or chunk_id is None or not realm_id:
            raise ValueError("bank parser correlation context is incomplete")

        candidate_attempt_id = uuid.uuid4()
        session = SessionLocal()
        try:
            # Trusted server-owned telemetry write. The row still carries the
            # authoritative tenant keys and ordinary reads remain FORCE-RLS scoped.
            from ..tenant_rls import tenant_scope_live

            with tenant_scope_live(session, bypass=True):
                delivery = (
                    session.query(BankStatementParseDeliveryAttempt)
                    .filter(
                        BankStatementParseDeliveryAttempt.id == delivery_attempt_id,
                        BankStatementParseDeliveryAttempt.bank_statement_import_id == import_id,
                        BankStatementParseDeliveryAttempt.realm_id == realm_id,
                    )
                    .one_or_none()
                )
                chunk_query = session.query(BankStatementParseChunk).filter(
                    BankStatementParseChunk.id == chunk_id,
                    BankStatementParseChunk.bank_statement_import_id == import_id,
                    BankStatementParseChunk.realm_id == realm_id,
                )
                if delivery is not None and delivery.chunk_id is None:
                    # Retained Wave-4A.1 one-shot evidence points from its ephemeral
                    # chunk to the delivery.  V2 deliberately reverses that edge so
                    # one logical chunk can own append-only delivery ordinals.
                    chunk_query = chunk_query.filter(
                        BankStatementParseChunk.delivery_attempt_id
                        == delivery_attempt_id,
                    )
                chunk = chunk_query.one_or_none()
                if delivery is None or chunk is None:
                    raise ValueError("bank parser delivery/chunk identity is not committed")
                organization_id = delivery.organization_id
                organization_company_id = delivery.organization_company_id
                if organization_id is None or organization_company_id is None:
                    raise ValueError("bank parser delivery lacks an authoritative tenant triple")
                if (
                    chunk.organization_id != organization_id
                    or chunk.organization_company_id != organization_company_id
                ):
                    raise ValueError("bank parser chunk tenant identity differs from delivery")

                is_resumable_v2 = delivery.chunk_id is not None
                if not is_resumable_v2:
                    raise ValueError("bank parser paid call lacks resumable-v2 identity")
                if (
                    delivery.chunk_id != chunk.id
                    or delivery.lifecycle_status != "started"
                    or chunk.lifecycle_status != "leased"
                    or chunk.application_retry_count is None
                ):
                    raise ValueError("bank parser v2 delivery/chunk lifecycle is not claimable")

                tenant = current_tenant_context()
                tenant_tag_succeeded = bool(
                    tenant is not None
                    and getattr(tenant, "organization_id", None) == organization_id
                    and getattr(tenant, "organization_company_id", None)
                    == organization_company_id
                    and str(getattr(tenant, "qbo_realm_id", "") or "").strip()
                    == realm_id
                )
                if not tenant_tag_succeeded:
                    raise ValueError("bank parser tenant attribution is incomplete")
                # The v2 schema requires the sole paid-call ledger row to exist
                # before the SDK call. This placeholder and provider envelope
                # commit atomically; post-call recording updates this exact row.
                placeholder_event = LLMUsageEvent(
                    organization_id=organization_id,
                    organization_company_id=organization_company_id,
                    qbo_realm_id=realm_id,
                    user_id=getattr(tenant, "user_id", None),
                    agent_run_id=task_ctx.run_id,
                    module="llm_lane",
                    route=lane,
                    task_type="bank_statement_parse",
                    step=task_ctx.step[:200] if task_ctx.step else None,
                    provider=provider,
                    model=model,
                    status="started",
                    tool_call_count=0,
                    tool_error_count=0,
                )
                session.add(placeholder_event)
                session.flush()
                attempt = BankStatementParseProviderAttempt(
                    id=candidate_attempt_id,
                    bank_statement_import_id=import_id,
                    delivery_attempt_id=delivery_attempt_id,
                    chunk_id=chunk_id,
                    realm_id=realm_id,
                    organization_id=organization_id,
                    organization_company_id=organization_company_id,
                    provider=provider,
                    model=model,
                    lane=lane,
                    status="started",
                    tenant_tag_succeeded=tenant_tag_succeeded,
                    attempt_ordinal=int(chunk.application_retry_count),
                    provider_call_id=candidate_attempt_id.hex,
                    lifecycle_status="started",
                    usage_event_id=placeholder_event.id,
                )
                session.add(attempt)
                session.commit()
                attempt_id = candidate_attempt_id
        finally:
            session.close()
        if trace is not None:
            trace.observe_call(attempt_id)
        return attempt_id
    except Exception as exc:
        if (
            candidate_attempt_id is not None
            and import_id is not None
            and delivery_attempt_id is not None
            and chunk_id is not None
            and realm_id
            and _bank_statement_provider_envelope_is_committed(
                attempt_id=candidate_attempt_id,
                import_id=import_id,
                delivery_attempt_id=delivery_attempt_id,
                chunk_id=chunk_id,
                realm_id=realm_id,
                provider=provider,
                model=model,
                lane=lane,
            )
        ):
            if trace is not None:
                trace.observe_call(candidate_attempt_id)
            return candidate_attempt_id
        if trace is not None:
            trace.observe_envelope_failure()
        logger.error(
            "[BankParseTelemetry] failed to persist provider envelope; call refused: %s",
            type(exc).__name__,
        )
        raise BankStatementProviderEnvelopeError(
            "bank parser provider envelope could not be committed"
        ) from exc


def finish_bank_statement_provider_attempt(
    provider_attempt_id: UUID | None,
    *,
    status: str,
    latency_ms: int,
    token_usage: Optional[Dict[str, Any]],
    output_truncated: bool | None,
    error: BaseException | None = None,
    expected_usage_event_id: int | None = None,
) -> bool:
    """Persist provider-returned evidence and verify a committed ledger link.

    Raw provider metrics are retained even when the best-effort ledger recorder
    fails. They are evidence only; ``LLMUsageEvent`` remains the sole cost ledger.
    Returns durable recorder proof and never raises into the feature path.
    """
    if provider_attempt_id is None:
        return False

    try:
        token_fields = extract_token_fields(token_usage)
        estimated_cost = None
        session = SessionLocal()
        try:
            from ..tenant_rls import tenant_scope_live

            with tenant_scope_live(session, bypass=True):
                attempt = (
                    session.query(BankStatementParseProviderAttempt)
                    .filter(BankStatementParseProviderAttempt.id == provider_attempt_id)
                    .one_or_none()
                )
                if attempt is None:
                    logger.warning(
                        "[BankParseTelemetry] provider attempt %s disappeared before finish",
                        provider_attempt_id,
                    )
                    return False

                if token_usage is not None:
                    from ..agent.performance import estimate_model_cost_usd

                    cost = estimate_model_cost_usd(attempt.model, token_usage)
                    estimated_cost = float(cost) if cost is not None else None

                if attempt.lifecycle_status is not None:
                    if attempt.lifecycle_status != "started":
                        logger.warning(
                            "[BankParseTelemetry] refusing to reopen terminal provider "
                            "attempt %s",
                            provider_attempt_id,
                        )
                        return False
                    if status == "ok":
                        attempt.lifecycle_status = (
                            "truncated" if output_truncated else "succeeded"
                        )
                    else:
                        attempt.lifecycle_status = (
                            "timed_out"
                            if error is not None and classify_error(error) == ERROR_TIMEOUT
                            else "failed"
                        )

                attempt.status = status
                attempt.input_tokens = token_fields["input_tokens"]
                attempt.output_tokens = token_fields["output_tokens"]
                attempt.cached_input_tokens = token_fields["cached_input_tokens"]
                attempt.cache_creation_input_tokens = token_fields[
                    "cache_creation_input_tokens"
                ]
                attempt.cache_read_input_tokens = token_fields["cache_read_input_tokens"]
                attempt.total_tokens = token_fields["total_tokens"]
                attempt.estimated_cost_usd = estimated_cost
                attempt.latency_ms = max(0, int(latency_ms))
                attempt.error_type = classify_error(error) if error is not None else None
                attempt.token_usage_reported = token_usage is not None
                attempt.cache_tokens_reported = any(
                    token_usage is not None and token_usage.get(key) is not None
                    for key in _CACHE_TOKEN_KEYS
                )
                attempt.output_truncated = output_truncated
                attempt.completed_at = datetime.now(timezone.utc)

                event = None
                if attempt.usage_event_id is not None:
                    event = (
                        session.query(LLMUsageEvent)
                        .filter(LLMUsageEvent.id == attempt.usage_event_id)
                        .one_or_none()
                    )
                attempt.recorder_succeeded = bool(
                    event is not None
                    and expected_usage_event_id is not None
                    and event.id == expected_usage_event_id
                    and event.task_type == "bank_statement_parse"
                    and event.provider == attempt.provider
                    and event.model == attempt.model
                    and event.organization_id == attempt.organization_id
                    and event.organization_company_id == attempt.organization_company_id
                    and event.qbo_realm_id == attempt.realm_id
                )
                session.commit()
                return bool(attempt.recorder_succeeded)
        finally:
            session.close()
    except Exception as exc:
        logger.warning(
            "[BankParseTelemetry] failed to finish provider attempt; feature result preserved: %s",
            type(exc).__name__,
        )
        return False


def _link_bank_statement_provider_attempt(
    session: Any,
    *,
    provider_attempt_id: UUID | None,
    event: LLMUsageEvent,
) -> None:
    """Attach one ledger row to one provider attempt without overwriting history."""
    if provider_attempt_id is None:
        return
    attempt = (
        session.query(BankStatementParseProviderAttempt)
        .filter(BankStatementParseProviderAttempt.id == provider_attempt_id)
        .one_or_none()
    )
    if attempt is None:
        logger.warning(
            "[BankParseTelemetry] usage event %s has no persisted provider attempt %s",
            event.id,
            provider_attempt_id,
        )
        return
    if attempt.usage_event_id is not None:
        logger.warning(
            "[BankParseTelemetry] provider attempt %s already has usage event %s",
            provider_attempt_id,
            attempt.usage_event_id,
        )
        return
    if event.qbo_realm_id != attempt.realm_id:
        logger.warning(
            "[BankParseTelemetry] refusing mismatched realm correlation for attempt %s",
            provider_attempt_id,
        )
        return
    attempt.usage_event_id = event.id


def _apply_final_usage_event(
    placeholder: LLMUsageEvent,
    final_event: LLMUsageEvent,
) -> None:
    """Finalize one already-authorized ledger row without changing identity."""

    placeholder.user_id = final_event.user_id
    placeholder.agent_run_id = final_event.agent_run_id
    placeholder.agent_step_id = final_event.agent_step_id
    placeholder.prompt_version = final_event.prompt_version
    placeholder.context_packet_version = final_event.context_packet_version
    placeholder.tool_bundle_name = final_event.tool_bundle_name
    placeholder.input_tokens = final_event.input_tokens
    placeholder.output_tokens = final_event.output_tokens
    placeholder.cached_input_tokens = final_event.cached_input_tokens
    placeholder.cache_creation_input_tokens = final_event.cache_creation_input_tokens
    placeholder.cache_read_input_tokens = final_event.cache_read_input_tokens
    placeholder.total_tokens = final_event.total_tokens
    placeholder.estimated_cost_usd = final_event.estimated_cost_usd
    placeholder.latency_ms = final_event.latency_ms
    placeholder.first_token_latency_ms = final_event.first_token_latency_ms
    placeholder.tool_call_count = final_event.tool_call_count
    placeholder.tool_error_count = final_event.tool_error_count
    placeholder.status = final_event.status
    placeholder.error_type = final_event.error_type
    placeholder.redacted_error = final_event.redacted_error


def _update_preallocated_usage_placeholder(
    session: Any,
    *,
    preallocated_usage_event_id: int | None,
    final_event: LLMUsageEvent,
) -> tuple[bool, int | None]:
    """Finalize one exact pre-call row; never fall through to a second insert."""

    if preallocated_usage_event_id is None:
        return False, None
    placeholder = (
        session.query(LLMUsageEvent)
        .filter(LLMUsageEvent.id == preallocated_usage_event_id)
        .with_for_update()
        .one_or_none()
    )
    if placeholder is None:
        logger.warning(
            "[LLMUsage] preallocated usage event %s disappeared before finalize",
            preallocated_usage_event_id,
        )
        return True, None
    if (
        placeholder.status != "started"
        or final_event.status not in {"ok", "error"}
        or placeholder.organization_id is None
        or placeholder.organization_company_id is None
        or placeholder.qbo_realm_id is None
        or placeholder.organization_id != final_event.organization_id
        or placeholder.organization_company_id
        != final_event.organization_company_id
        or placeholder.qbo_realm_id != final_event.qbo_realm_id
        or placeholder.module != final_event.module
        or placeholder.route != final_event.route
        or placeholder.task_type != final_event.task_type
        or placeholder.step != final_event.step
        or placeholder.provider != final_event.provider
        or placeholder.model != final_event.model
    ):
        logger.warning(
            "[LLMUsage] refusing mismatched preallocated usage finalize for event %s",
            preallocated_usage_event_id,
        )
        return True, None
    _apply_final_usage_event(placeholder, final_event)
    session.flush()
    return True, placeholder.id


def _update_bank_statement_usage_placeholder(
    session: Any,
    *,
    provider_attempt_id: UUID | None,
    final_event: LLMUsageEvent,
) -> tuple[bool, int | None]:
    """Update the sole v2 ledger row; return ``(handled, event_id)``.

    Legacy one-shot attempts have no lifecycle and keep the insert+link behavior.
    A malformed v2 link is handled-but-failed so the recorder never creates a
    second row that could masquerade as the physical call's authoritative ledger.
    """
    if provider_attempt_id is None:
        return False, None
    attempt = (
        session.query(BankStatementParseProviderAttempt)
        .filter(BankStatementParseProviderAttempt.id == provider_attempt_id)
        .one_or_none()
    )
    if attempt is None or attempt.lifecycle_status is None:
        return False, None
    if attempt.usage_event_id is None:
        logger.warning(
            "[BankParseTelemetry] v2 provider attempt %s has no ledger placeholder",
            provider_attempt_id,
        )
        return True, None
    placeholder = (
        session.query(LLMUsageEvent)
        .filter(LLMUsageEvent.id == attempt.usage_event_id)
        .one_or_none()
    )
    if placeholder is None:
        logger.warning(
            "[BankParseTelemetry] v2 provider attempt %s lost ledger placeholder %s",
            provider_attempt_id,
            attempt.usage_event_id,
        )
        return True, None
    if (
        placeholder.status != "started"
        or placeholder.task_type != "bank_statement_parse"
        or placeholder.provider != attempt.provider
        or placeholder.model != attempt.model
        or placeholder.route != attempt.lane
        or placeholder.organization_id != attempt.organization_id
        or placeholder.organization_company_id != attempt.organization_company_id
        or placeholder.qbo_realm_id != attempt.realm_id
        or final_event.provider != attempt.provider
        or final_event.model != attempt.model
        or final_event.route != attempt.lane
        or final_event.organization_id != attempt.organization_id
        or final_event.organization_company_id != attempt.organization_company_id
        or final_event.qbo_realm_id != attempt.realm_id
    ):
        logger.warning(
            "[BankParseTelemetry] refusing mismatched v2 ledger update for attempt %s",
            provider_attempt_id,
        )
        return True, None

    _apply_final_usage_event(placeholder, final_event)
    session.flush()
    return True, placeholder.id


def record_llm_usage(
    *,
    provider: Optional[str],
    model: Optional[str],
    status: str = "ok",
    latency_ms: Optional[int] = None,
    first_token_latency_ms: Optional[int] = None,
    token_usage: Optional[Dict[str, Any]] = None,
    error: Optional[BaseException] = None,
    error_type: Optional[str] = None,
    error_text: Optional[str] = None,
    organization_id: Optional[int] = None,
    organization_company_id: Optional[int] = None,
    qbo_realm_id: Optional[str] = None,
    user_id: Optional[int] = None,
    agent_run_id: Optional[int] = None,
    agent_step_id: Optional[int] = None,
    module: Optional[str] = None,
    route: Optional[str] = None,
    task_type: Optional[str] = None,
    step: Optional[str] = None,
    prompt_version: Optional[str] = None,
    context_packet_version: Optional[str] = None,
    tool_bundle_name: Optional[str] = None,
    tool_call_count: int = 0,
    tool_error_count: int = 0,
    estimated_cost_usd: Optional[float] = None,
    provider_attempt_id: UUID | None = None,
    preallocated_usage_event_id: int | None = None,
    db: Optional[Any] = None,
) -> Optional[int]:
    """Write a single LLMUsageEvent. Best-effort: never raises.

    Returns the new event id, or None if recording was skipped/failed.
    A provided ``db`` session is used (and left open for the caller to
    commit); otherwise a short-lived SessionLocal is opened and committed.
    """
    try:
        # PLAN-EXEMPT: ratified plan 20260702-qa-remediation W-1 (F-7 ledger dims).
        # Work-unit attribution: an explicit task_type wins; else the bound LLM
        # task context (bind_llm_task_context — the close walk / nightly engine /
        # briefing binders); else the interactive agent loop is "chat" and
        # everything unclassified is honestly "other". run_id/step backfill the
        # same way so run-scoped budget accounting sees every underneath call.
        from .llm_task_context import (
            DEFAULT_TASK_TYPE,
            current_llm_task_context,
            normalize_task_type,
        )

        task_ctx = current_llm_task_context()
        if task_ctx is not None:
            if task_type is None:
                task_type = task_ctx.task_type
            if agent_run_id is None:
                agent_run_id = task_ctx.run_id
            if step is None:
                step = task_ctx.step
        if task_type is None:
            task_type = "chat" if module == "agent_runtime" else DEFAULT_TASK_TYPE
        task_type = normalize_task_type(task_type)

        if error is not None and not error_type:
            error_type = classify_error(error)
        if error is not None and status == "ok":
            status = "error"

        raw_error_text = error_text
        if raw_error_text is None and error is not None:
            raw_error_text = str(error)
        redacted = redact_error(raw_error_text) if raw_error_text else None

        token_fields = extract_token_fields(token_usage)

        event = LLMUsageEvent(
            organization_id=organization_id,
            organization_company_id=organization_company_id,
            qbo_realm_id=qbo_realm_id,
            user_id=user_id,
            agent_run_id=agent_run_id,
            agent_step_id=agent_step_id,
            module=module,
            route=route,
            task_type=task_type,
            step=step[:200] if step else None,
            provider=provider,
            model=model,
            prompt_version=prompt_version,
            context_packet_version=context_packet_version,
            tool_bundle_name=tool_bundle_name,
            input_tokens=token_fields["input_tokens"],
            output_tokens=token_fields["output_tokens"],
            cached_input_tokens=token_fields["cached_input_tokens"],
            cache_creation_input_tokens=token_fields["cache_creation_input_tokens"],
            cache_read_input_tokens=token_fields["cache_read_input_tokens"],
            total_tokens=token_fields["total_tokens"],
            estimated_cost_usd=estimated_cost_usd,
            latency_ms=latency_ms,
            first_token_latency_ms=first_token_latency_ms,
            tool_call_count=tool_call_count or 0,
            tool_error_count=tool_error_count or 0,
            status=status,
            error_type=error_type,
            redacted_error=redacted,
        )

        if db is not None:
            handled, existing_event_id = _update_preallocated_usage_placeholder(
                db,
                preallocated_usage_event_id=preallocated_usage_event_id,
                final_event=event,
            )
            if handled:
                return existing_event_id
            handled, existing_event_id = _update_bank_statement_usage_placeholder(
                db,
                provider_attempt_id=provider_attempt_id,
                final_event=event,
            )
            if handled:
                return existing_event_id
            db.add(event)
            db.flush()
            _link_bank_statement_provider_attempt(
                db,
                provider_attempt_id=provider_attempt_id,
                event=event,
            )
            db.flush()
            return event.id

        session = SessionLocal()
        try:
            # PLAN-EXEMPT: live P0 — token/cost telemetry insert rejected by FORCE RLS.
            # This is a non-request, server-only telemetry write: the lane/context path
            # binds no tenant GUC (and the realm triple is often NULL), so the org-keyed
            # WITH CHECK on llm_usage_events fails closed and every usage row errors.
            # Emit the audited rls_bypass for this trusted server-owned write — correct
            # regardless of a NULL tenant; real org/realm attribution (when the caller
            # knew it) is still carried on the row's own columns. Keep the post-commit
            # refresh INSIDE the block so its re-read runs under the same bypass scope
            # (the begin-listener re-emits the pinned ContextVar on the new transaction).
            from ..tenant_rls import tenant_scope_live
            with tenant_scope_live(session, bypass=True):
                handled, existing_event_id = _update_preallocated_usage_placeholder(
                    session,
                    preallocated_usage_event_id=preallocated_usage_event_id,
                    final_event=event,
                )
                if handled:
                    session.commit()
                    return existing_event_id
                handled, existing_event_id = _update_bank_statement_usage_placeholder(
                    session,
                    provider_attempt_id=provider_attempt_id,
                    final_event=event,
                )
                if handled:
                    session.commit()
                    return existing_event_id
                session.add(event)
                session.flush()
                _link_bank_statement_provider_attempt(
                    session,
                    provider_attempt_id=provider_attempt_id,
                    event=event,
                )
                session.commit()
                session.refresh(event)
            return event.id
        finally:
            session.close()
    except Exception as exc:  # pragma: no cover - defensive guard
        # FAIL-OPEN but LOUD (W-1): metering must never break the feature, but
        # a dropped ledger row is unmetered spend — warn, never debug-whisper.
        logger.warning("[LLMUsage] failed to record usage event (unmetered call!): %s", exc)
        try:
            if db is not None:
                db.rollback()
        except Exception:
            pass
        return None

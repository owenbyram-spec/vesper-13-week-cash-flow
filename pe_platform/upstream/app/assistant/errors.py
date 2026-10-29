"""Typed assistant runtime errors mapped to correct HTTP status codes.

Each subclass is a SEMANTIC label for a distinct prep/runtime condition, carrying the public
envelope fields (``http_status`` / ``error_code`` / ``user_message`` / ``retryable``) as class
attributes. The handler maps by the concrete subclass — replacing the historical
``ValueError -> 429`` collapse in ``app/assistant/main.py``.

They subclass ``ValueError`` for back-compat: any existing ``except ValueError`` keeps working.
Because of that, the handler MUST catch ``except AssistantError`` BEFORE any ``except ValueError``
fallback (a more-specific arm first), or the subclass is re-collapsed to the generic arm.

``user_message`` is PUBLIC and safe to show a client / persist in a conversation — never put a
secret, token, raw exception, or PII in it.
"""
from __future__ import annotations


class AssistantError(ValueError):
    """Base for assistant prep/runtime errors. Maps to an HTTP status at the handler boundary."""

    http_status: int = 500
    error_code: str = "assistant_error"
    user_message: str = "The assistant encountered an error. Please try again."
    retryable: bool = False

    def __init__(self, message: str | None = None) -> None:
        # Preserve any caller-supplied (internal) message for logs/back-compat; the client only
        # ever sees ``user_message`` via the error envelope.
        super().__init__(message or self.user_message)


class AssistantRequestTooLarge(AssistantError):
    """The request text exceeds the configured context limit (413)."""

    http_status = 413
    error_code = "assistant_request_too_large"
    user_message = "Your message is too long. Please shorten it and try again."
    retryable = False


class AssistantDuplicateRun(AssistantError):
    """A matching agent run is already in progress (409)."""

    http_status = 409
    error_code = "assistant_duplicate_run"
    user_message = "A request is already in progress. Please wait for it to finish."
    retryable = True


class AssistantCompanySuspended(AssistantError):
    """The company's assistant access is suspended by admin/billing (403)."""

    http_status = 403
    error_code = "assistant_company_suspended"
    user_message = "This company's assistant access is suspended. Please contact your administrator."
    retryable = False


class AssistantQuotaExceeded(AssistantError):
    """The tenant's monthly agent token limit has been reached (429)."""

    http_status = 429
    error_code = "assistant_quota_exceeded"
    user_message = "The monthly AI token quota for this workspace has been reached."
    retryable = False


class AssistantBudgetReached(AssistantError):
    """The tenant's monthly AI budget has been reached (429)."""

    http_status = 429
    error_code = "assistant_budget_reached"
    user_message = "Monthly AI budget reached for this workspace — contact your admin."
    retryable = False


class AssistantBudgetUnavailable(AssistantError):
    """Budget evidence could not be read; no model spending is authorized (503)."""

    http_status = 503
    error_code = "assistant_budget_unavailable"
    user_message = "AI budget information is temporarily unavailable. Please try again shortly."
    retryable = True


class AssistantProviderRateLimited(AssistantError):
    """The upstream model provider rate-limited the request (429) — transient, retry later."""

    http_status = 429
    error_code = "assistant_provider_rate_limited"
    user_message = "The AI service is temporarily busy. Please try again shortly."
    retryable = True

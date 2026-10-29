from __future__ import annotations

import logging
from typing import Any
from uuid import uuid4

from fastapi import HTTPException

from .redaction import redact_to_string

logger = logging.getLogger(__name__)


def error_envelope(
    *,
    error_code: str,
    user_message: str,
    trace_id: str | None = None,
    retryable: bool = False,
    support_hint: str | None = None,
) -> dict[str, Any]:
    return {
        "error_code": error_code,
        "user_message": user_message,
        "trace_id": trace_id or uuid4().hex,
        "retryable": retryable,
        **({"support_hint": support_hint} if support_hint else {}),
    }


def safe_error_detail(
    *,
    error_code: str,
    user_message: str,
    exc: Exception | None = None,
    retryable: bool = False,
    support_hint: str | None = None,
    log_context: str = "api_error",
) -> dict[str, Any]:
    detail = error_envelope(
        error_code=error_code,
        user_message=user_message,
        retryable=retryable,
        support_hint=support_hint,
    )
    if exc is not None:
        logger.warning(
            "%s trace_id=%s error=%s",
            log_context,
            detail["trace_id"],
            redact_to_string(str(exc), max_length=500),
        )
    return detail


def user_safe_http_exception(
    *,
    status_code: int,
    error_code: str,
    user_message: str,
    exc: Exception | None = None,
    retryable: bool = False,
    support_hint: str | None = None,
    log_context: str = "api_error",
) -> HTTPException:
    detail = safe_error_detail(
        error_code=error_code,
        user_message=user_message,
        exc=exc,
        retryable=retryable,
        support_hint=support_hint,
        log_context=log_context,
    )
    return HTTPException(status_code=status_code, detail=detail)

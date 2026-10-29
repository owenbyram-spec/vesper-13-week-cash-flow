"""Per-account failed-login backoff (defense-in-depth beside the slowapi limiter).

The slowapi bucket for /auth/login keys on client-supplied material (the auth
cookie or the raw Authorization header) with an IP fallback that is shared
behind the frontend /bff proxy, so a credential-stuffing attacker can rotate
the header to mint a fresh bucket per request. This counter keys on the TARGET
ACCOUNT (normalized email) instead — the one thing the attacker cannot rotate
while still attacking that account.

Fail-open by design: the counter rides the canonical cache seam
(app/services/cache.py), where any Redis error is swallowed into a miss — an
infra outage must never lock out logins. Only 401s are counted; MFA challenges
(202), org selection (409), and forced password resets (403) are legitimate
flow statuses and never increment the counter. A successful login clears it.
"""
import hashlib
import os

from fastapi import HTTPException, status

from .cache import delete_cached, get_cached, set_cached

_FAILED_LOGIN_MAX = 5
_FAILED_LOGIN_WINDOW_SECONDS = 60
# Mirrors rate_limit._LIMITER_ENABLED: the suite's repeated failed logins must
# not trip the backoff; the dedicated backoff test re-enables it via monkeypatch.
_BACKOFF_ENABLED = os.getenv("ENVIRONMENT", "").lower() != "test"


def _key(email: str) -> str:
    digest = hashlib.sha256((email or "").strip().lower().encode("utf-8")).hexdigest()
    return f"auth:login-fail:{digest}"


def enforce_failed_login_backoff(email: str) -> None:
    """Raise 429 when the account has exhausted its failed-attempt budget."""
    if not _BACKOFF_ENABLED:
        return
    count = get_cached(_key(email))
    if isinstance(count, int) and count >= _FAILED_LOGIN_MAX:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many failed login attempts for this account. Try again in a minute.",
        )


def record_failed_login(email: str) -> None:
    """Count one failed (401) login attempt against the account's window."""
    if not _BACKOFF_ENABLED:
        return
    count = get_cached(_key(email))
    next_count = (count if isinstance(count, int) else 0) + 1
    set_cached(_key(email), next_count, ttl_seconds=_FAILED_LOGIN_WINDOW_SECONDS)


def clear_failed_login_backoff(email: str) -> None:
    """Reset the account's window after a successful login."""
    if not _BACKOFF_ENABLED:
        return
    delete_cached(_key(email))

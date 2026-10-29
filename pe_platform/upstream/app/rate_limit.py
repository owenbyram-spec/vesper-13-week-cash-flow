"""Shared slowapi limiter.

Lives in its own module (not main.py) so routers can apply ``@limiter.limit``
decorators without importing main.py (which would be circular — main imports the
routers). main.py attaches ``limiter`` to ``app.state``, registers the
RateLimitExceeded handler and installs the SlowAPI middleware, so
``default_limits`` applies to every route that has no ``@limiter.limit`` of its
own (health checks and provider webhooks are exempted there).

Bucket keys never use request-supplied query parameters or unvalidated header
material, either of which a caller could vary to mint a fresh bucket:

- a request carrying a validated session is keyed on the user — a session JWT
  signed with SECRET_KEY, or a Stytch session token the auth edge has already
  validated and cached;
- anything else is keyed on the client IP: the address the frontend BFF attests
  with an HMAC (``BFF_CLIENT_IP_SECRET``), else the socket peer, which uvicorn
  derives from X-Forwarded-For only for proxies in FORWARDED_ALLOW_IPS.

Every browser request reaches the backend from the BFF, so without the signed
header all unauthenticated users would share one bucket. The pre-session
credential routes also get a per-account bucket (``enforce_account_rate_limit``).
"""
import hashlib
import hmac
import ipaddress
import os
import time

import jwt
from fastapi import HTTPException, Request
from jwt.exceptions import InvalidTokenError
from limits import parse
from limits.errors import StorageError
from limits.storage import MemoryStorage
from limits.strategies import FixedWindowRateLimiter
from redis.exceptions import RedisError
from slowapi import Limiter

from . import config as app_config

CLIENT_IP_HEADER = "x-vesper-bot-client-ip"
CLIENT_IP_SIGNATURE_HEADER = "x-vesper-bot-client-ip-signature"
CLIENT_IP_SIGNATURE_MAX_AGE_SECONDS = 300

# Per-account budgets for the pre-session credential routes. They mirror the
# per-client limits declared on the same routes in app/auth.py and apply beside
# them: a shared office IP does not lock other accounts out, and requests aimed
# at one account from many IPs share that account's budget.
ACCOUNT_RATE_LIMITS = {
    "/auth/login": "5/minute",
    "/auth/password-reset/start": "10/minute",
    "/auth/password-reset/complete": "10/minute",
    "/auth/mfa/totp/start-challenge": "10/minute",
    "/auth/mfa/totp/verify": "15/minute",
    "/auth/mfa/sms/send-challenge": "5/minute",
    "/auth/mfa/sms/verify": "15/minute",
}


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def client_ip_signature(secret: str, client_ip: str, timestamp: int) -> str:
    """``v1=<unix seconds>.<hex HMAC-SHA256 of "v1.<timestamp>.<client ip>">``.

    frontend/app/bff/[...path]/route.ts computes the same value.
    """
    message = f"v1.{timestamp}.{client_ip}".encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()
    return f"v1={timestamp}.{digest}"


def _signed_client_ip(request: Request) -> str | None:
    """The BFF-attested client IP, or None unless its signature verifies."""
    secret = app_config.settings.bff_client_ip_secret
    claimed = (request.headers.get(CLIENT_IP_HEADER) or "").strip()
    signature = (request.headers.get(CLIENT_IP_SIGNATURE_HEADER) or "").strip()
    if not secret or not claimed or not signature:
        return None
    try:
        client_ip = str(ipaddress.ip_address(claimed))
    except ValueError:
        return None
    version, _, rest = signature.partition("=")
    timestamp, _, _ = rest.partition(".")
    if version != "v1" or not timestamp.isdigit():
        return None
    if abs(time.time() - int(timestamp)) > CLIENT_IP_SIGNATURE_MAX_AGE_SECONDS:
        return None
    if not hmac.compare_digest(client_ip_signature(secret, claimed, int(timestamp)), signature):
        return None
    return client_ip


def trusted_client_ip(request: Request) -> str:
    return _signed_client_ip(request) or (request.client.host if request.client else "unknown")


def _validated_user_id(request: Request) -> int | None:
    """The user behind a validated session token on the request, if any.

    Only validation the auth edge itself relies on counts: a SECRET_KEY-signed
    session JWT, or a Stytch session token present in the auth edge's
    validated-session cache. An unvalidated token is ignored, never hashed.
    """
    # Imported here: both modules import app.models, and routers import this
    # module while app.models may still be loading.
    from .auth_session import ALGORITHM, SECRET_KEY
    from .services.stytch_b2b import get_cached_session_identity

    scheme, _, bearer = (request.headers.get("authorization") or "").partition(" ")
    candidates = (
        bearer.strip() if scheme.lower() == "bearer" else "",
        request.cookies.get(app_config.settings.auth_cookie_name) or "",
    )
    for token in candidates:
        if not token:
            continue
        try:
            return int(jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM]).get("sub"))
        except (InvalidTokenError, TypeError, ValueError):
            pass
        cached = get_cached_session_identity(token)
        if cached:
            return cached["user_id"]
    return None


def _rate_limit_key(request: Request) -> str:
    user_id = _validated_user_id(request)
    if user_id is not None:
        return f"user:{user_id}"
    return "ip:" + _digest(trusted_client_ip(request))


def pull_api_rate_limit_key(request: Request) -> str:
    """Rate-limit the Pull API PER API KEY, not per IP — external AI agents may
    share an egress IP, so keying on the bearer key prevents one tenant's burst
    from exhausting another's budget."""
    bearer = request.headers.get("authorization") or "anonymous"
    return "pull:" + hashlib.sha256(bearer.encode("utf-8")).hexdigest()[:16]


def _submitted_account(body: object) -> str | None:
    if not isinstance(body, dict):
        return None
    email = body.get("email")
    if isinstance(email, str) and email.strip():
        return "email:" + email.strip().lower()
    member_id = body.get("member_id")
    if isinstance(member_id, str) and member_id.strip():
        return f"member:{body.get('organization_id')}:{member_id.strip()}"
    return None


async def enforce_account_rate_limit(request: Request) -> None:
    """Per-account bucket for the credential routes in ACCOUNT_RATE_LIMITS.

    Mounted as a dependency on the auth router. FastAPI has already parsed the
    JSON body by the time dependencies run, so reading it here is free.
    """
    limit = ACCOUNT_RATE_LIMITS.get(request.url.path)
    if limit is None or request.method != "POST" or not limiter.enabled:
        return
    try:
        body = await request.json()
    except ValueError:  # not JSON: the route's own validation answers it
        return
    account = _submitted_account(body)
    if account is None:
        return
    item, key = parse(limit), "account:" + _digest(account)
    try:
        allowed = limiter.limiter.hit(item, key, request.url.path)
    except (RedisError, StorageError):  # shared storage unreachable: keep counting in this process
        allowed = _ACCOUNT_FALLBACK.hit(item, key, request.url.path)
    if not allowed:
        raise HTTPException(status_code=429, detail="Too many attempts for this account. Try again in a minute.")


# Shared Redis storage so per-route counters are consistent across uvicorn
# workers (WEB_CONCURRENCY>1) — in-memory is per-process and silently
# under-enforces; falls back to in-memory when REDIS_URL is unset (dev).
# If Redis becomes unreachable the limiter keeps enforcing every limit with
# per-process memory (rechecking Redis as it goes) rather than failing each
# request: with the middleware installed, a storage error would otherwise
# turn every route into a 500.
# Disabled under the test env so the suite's repeated logins aren't 429'd; the
# dedicated 429 tests re-enable it.
_LIMITER_ENABLED = os.getenv("ENVIRONMENT", "").lower() != "test"
limiter = Limiter(default_limits=["120/minute"], key_func=_rate_limit_key, storage_uri=os.getenv("REDIS_URL") or None,
                  enabled=_LIMITER_ENABLED, in_memory_fallback_enabled=True)
_ACCOUNT_FALLBACK = FixedWindowRateLimiter(MemoryStorage())

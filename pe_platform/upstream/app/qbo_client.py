from typing import Optional, Dict, Any
from collections.abc import Callable, Iterator
# PLAN-EXEMPT: chunk C6 (chat-vs-background contention, AC-7) of ratified plan
# 20260706-agent-path-remediation — the C6 dispatch declares app/qbo_client.py as
# its write surface; the active-plan surface_scope[] glob list omits this one file.
from contextvars import ContextVar
import base64
import contextlib
import json
import logging
import os
import random
import math
import re
import time
import uuid
from datetime import datetime, timedelta

import redis
import requests
from sqlalchemy import text as sa_text
from sqlalchemy.exc import OperationalError
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from fastapi import HTTPException

from .config import settings
from .models import QBOToken
# PLAN-EXEMPT: QBO connection-health revocation-detection — the reconnect-required signal.
# PLAN-EXEMPT: ratified plan 20260713-wave2-1-ar-collections C1 (marker
# RATIFICATION-2026-07-13.md) — typed-uncertain AR write methods (decision 4 / E2).
from .qbo_errors import QBOUncertainOutcomeError, qbo_not_connected_exception, qbo_reconnect_required_exception
from .services.crypto import decrypt_secret, encrypt_secret
from .services.redaction import redact_sensitive, redact_to_string  # noqa: F401
from .services.tenant_companies import assert_qbo_token_company_scope, organization_for_company
from .tenant_rls import tenant_scope_live

REQUEST_TIMEOUT_SECONDS = 30
logger = logging.getLogger(__name__)

_qbo_review_budget: ContextVar = ContextVar("qbo_review_budget", default=None)

# ----------------------------------------------------------------------
# QBO THROTTLING SAFETY (Intuit limits are PER REALM: 500 req/min, 10 concurrent)
# ----------------------------------------------------------------------
# B3.1 — per-REQUEST bounded backoff. A transient 429/5xx is absorbed by
# retrying the SINGLE call, so a multi-entity sync task is never restarted for a
# throttle blip. Reuses the existing ``parse_qbo_fault(...).retryable`` signal
# (429 or >=500). Honors ``Retry-After`` (Intuit returns it on 429/003001) and
# otherwise backs off exponentially with full jitter, capped at a max attempt
# count so a persistently-failing call still surfaces instead of looping.
QBO_MAX_REQUEST_ATTEMPTS = 4          # 1 initial + 3 retries
QBO_BACKOFF_BASE_SECONDS = 1.0        # exponential base: 1s, 2s, 4s, …
QBO_BACKOFF_MAX_SECONDS = 30.0        # ceiling on any single backoff sleep
QBO_RETRY_AFTER_MAX_SECONDS = 60.0    # ceiling on a server-supplied Retry-After

# B3.2 — per-REALM concurrency limiter. Intuit allows 10 concurrent calls per
# realm; we cap at 8 to leave headroom. A Redis semaphore keyed on realm_id is
# acquired before each QBO request and released in ``finally``. Degrades safely:
# no Redis → no limiter (a single worker can never breach 10 on its own).
QBO_REALM_MAX_CONCURRENT = 8
QBO_SEMAPHORE_SLOT_TTL_SECONDS = 90   # a slot self-expires if a holder dies mid-call
QBO_SEMAPHORE_ACQUIRE_TIMEOUT = 60.0  # how long to wait for a free slot
QBO_SEMAPHORE_POLL_SECONDS = 0.1

# PLAN-EXEMPT: chunk C6 (AC-7.1) of ratified plan 20260706-agent-path-remediation —
# app/qbo_client.py is C6's declared write surface; active-plan surface_scope omits it.
# C6 — lane-aware interactive headroom. Background/batch work (the Celery sync +
# objective/warm tasks) must not starve interactive chat on the same realm:
# background callers acquire against a REDUCED cap, so
# ``QBO_REALM_INTERACTIVE_RESERVED`` slots of the shared pool are claimable ONLY
# by the interactive lane. Both lanes share ONE pool (one ZSET), so total
# concurrency never exceeds QBO_REALM_MAX_CONCURRENT=8 — comfortably inside
# Intuit's 10-concurrent per-realm limit.
QBO_LANE_INTERACTIVE = "interactive"
QBO_LANE_BACKGROUND = "background"


def _env_int(name: str, default: int) -> int:
    """Guarded env-int read: a malformed value logs + falls back, never raises
    (the bare ``int(os.getenv(...))`` import-crash class)."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        logger.warning("Ignoring non-integer env %s; using default %d", name, default)
        return default


# Slots reserved for the interactive lane (env-tunable), clamped so the
# background lane always keeps >= 1 claimable slot.
QBO_REALM_INTERACTIVE_RESERVED = max(
    0, min(_env_int("QBO_REALM_INTERACTIVE_RESERVED", 2), QBO_REALM_MAX_CONCURRENT - 1)
)

# Context-scoped lane override — the module-level seam for callers that cannot
# thread a ``lane=`` parameter down to the client (e.g. a batch entrypoint that
# wants everything under it tagged background): ``with qbo_lane(QBO_LANE_BACKGROUND): ...``.
_qbo_lane_override: "ContextVar[str | None]" = ContextVar("qbo_lane_override", default=None)


@contextlib.contextmanager
def qbo_lane(lane: str) -> Iterator[None]:
    """Tag every ``qbo_realm_semaphore`` acquire inside this context with ``lane``."""
    token = _qbo_lane_override.set(lane)
    try:
        yield
    finally:
        _qbo_lane_override.reset(token)


def _running_in_celery_task() -> bool:
    """True when the current call stack is inside a Celery task body.

    This is the zero-touch background seam: every Vesper Bot background/batch QBO
    caller (``sync_all_data``, the objective runners in app/tasks/agent.py, the
    snapshot warm tasks) executes inside a Celery task, and no interactive
    request-thread caller does. Degrades to False (interactive — today's
    behavior) if Celery introspection fails for any reason.
    """
    try:
        from celery import current_task
        return bool(current_task)
    except Exception:  # noqa: BLE001 — lane detection is best-effort; default to interactive
        return False


def _resolve_qbo_lane(explicit: "str | None") -> str:
    """Lane precedence: explicit param > ``qbo_lane`` context override >
    Celery-task detection > interactive. The interactive default preserves every
    untouched caller's semantics (the full cap)."""
    if explicit in (QBO_LANE_INTERACTIVE, QBO_LANE_BACKGROUND):
        return explicit
    override = _qbo_lane_override.get()
    if override in (QBO_LANE_INTERACTIVE, QBO_LANE_BACKGROUND):
        return override
    return QBO_LANE_BACKGROUND if _running_in_celery_task() else QBO_LANE_INTERACTIVE


def _lane_cap(lane: str) -> int:
    """Claimable-slot ceiling for a lane over the ONE shared per-realm pool.
    Background can never push the pool past (max - reserved), so the reserved
    slots stay claimable by interactive even under full background load."""
    if lane == QBO_LANE_BACKGROUND:
        return max(QBO_REALM_MAX_CONCURRENT - QBO_REALM_INTERACTIVE_RESERVED, 1)
    return QBO_REALM_MAX_CONCURRENT

# B3.3 — per-REALM sync lock. A second ``sync_all_data`` for the same realm
# while one is running is wasted QBO load (upserts are idempotent, so it is not
# corruption — just throttle pressure). A Redis SETNX lock with a TTL makes the
# second trigger a no-op until the first releases (or the TTL lapses if it dies).
QBO_SYNC_LOCK_TTL_SECONDS = 3600      # generous: a full sync can be slow


def _qbo_redis() -> "redis.Redis | None":
    """Degrade-safe Redis client for the throttling primitives.

    Mirrors the cache module's posture: a missing/unreachable Redis returns
    ``None`` and the caller no-ops the limiter/lock rather than crashing the
    sync. Each call constructs a short-timeout client; the connection pool is
    process-local and cheap, and a dead Redis must NEVER take the sync down.
    """
    if not settings.redis_url:
        return None
    try:
        client = redis.Redis.from_url(
            settings.redis_url, socket_timeout=2, socket_connect_timeout=2
        )
        client.ping()
        return client
    except Exception as exc:  # noqa: BLE001 — Redis is best-effort here; degrade, never crash the sync
        logger.warning("QBO throttling Redis unavailable; limiter/lock disabled: %s", redact_to_string(str(exc), max_length=200))
        return None


def _compute_backoff_seconds(attempt: int, resp: requests.Response) -> float:
    """Seconds to wait before the next attempt.

    ``Retry-After`` (when the server supplies it on a 429) wins; otherwise full
    exponential backoff with jitter, both bounded. ``attempt`` is 1-based (the
    attempt that just failed).
    """
    headers = getattr(resp, "headers", None) or {}
    retry_after = headers.get("Retry-After") or headers.get("retry-after")
    if retry_after:
        try:
            return min(float(retry_after), QBO_RETRY_AFTER_MAX_SECONDS)
        except (TypeError, ValueError):
            pass  # non-numeric (HTTP-date form) → fall through to exponential backoff
    capped = min(QBO_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)), QBO_BACKOFF_MAX_SECONDS)
    return random.uniform(0, capped)  # full jitter


def _request_with_qbo_retry(
    send: "Callable[[], requests.Response]", *, retry_5xx: bool = True
) -> "requests.Response":
    """Send a QBO request, retrying the SINGLE call on a retryable fault.

    ``send`` performs ONE ``requests`` call and returns its Response (it must not
    raise for an HTTP error status — status handling stays with the caller). A
    429 is always retried with bounded backoff (rate-limited means the request
    never reached processing, so it is safe to repeat even for a write). A >=500
    is retried only when ``retry_5xx`` is True — the READ methods set it True; the
    WRITE methods (JE create / update / void) set it False so a 5xx that may have
    already mutated the General Ledger is NOT silently repeated (the upstream
    idempotency ledger is the dedupe, but we still avoid a needless re-POST).
    Any other status (incl. 401, 4xx) returns immediately for the caller's normal
    handling. A network-level ``RequestException`` is retried up to the attempt
    cap, then re-raised.
    """
    last_exc: "Exception | None" = None
    for attempt in range(1, QBO_MAX_REQUEST_ATTEMPTS + 1):
        try:
            resp = send()
        except requests.RequestException as exc:
            last_exc = exc
            if attempt >= QBO_MAX_REQUEST_ATTEMPTS:
                raise
            time.sleep(min(QBO_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)), QBO_BACKOFF_MAX_SECONDS))
            continue
        status = getattr(resp, "status_code", None)
        is_429 = status == 429
        is_5xx = isinstance(status, int) and status >= 500
        retryable = is_429 or (is_5xx and retry_5xx)
        if not retryable or attempt >= QBO_MAX_REQUEST_ATTEMPTS:
            return resp
        delay = _compute_backoff_seconds(attempt, resp)
        logger.warning(
            "QBO request retryable fault: status=%s attempt=%s/%s backoff=%.2fs",
            status, attempt, QBO_MAX_REQUEST_ATTEMPTS, delay,
        )
        time.sleep(delay)
    # Unreachable (the loop returns or raises), but satisfy the type checker.
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("QBO request retry loop exited without a response")


# PLAN-EXEMPT: chunk C6 (AC-7.1) of ratified plan 20260706-agent-path-remediation —
# app/qbo_client.py is C6's declared write surface; active-plan surface_scope omits it.
@contextlib.contextmanager
def qbo_realm_semaphore(realm_id: str, lane: "str | None" = None) -> Iterator[None]:
    """Per-realm concurrency gate (≤ QBO_REALM_MAX_CONCURRENT, keyed on realm_id).

    Acquires one slot of a Redis-backed counting semaphore before yielding and
    releases it in ``finally`` (even on exception). Stale slots self-heal via a
    per-slot TTL, so a crashed holder cannot permanently consume a slot. Degrades
    to a no-op when Redis is unavailable (a lone worker cannot exceed 10 on its
    own; the cap matters across the worker fleet). PER-REALM, never app-wide —
    an app-wide gate would throttle every tenant together.

    ``lane`` (C6): ``"background"`` callers claim slots only while the pool is
    below the reduced background cap; ``"interactive"`` callers may use the full
    cap — so QBO_REALM_INTERACTIVE_RESERVED slots always remain claimable by
    chat even when background sync/objective fan-out saturates its lane.
    ``None`` resolves via ``_resolve_qbo_lane`` (context override, then
    Celery-task detection, then interactive — untouched request-path callers
    keep today's full-cap behavior).
    """
    review = _qbo_review_budget.get()
    if review is not None:
        with _review_realm_semaphore(realm_id, lane, review[0]):
            yield
        return

    client = _qbo_redis()
    if client is None:
        yield
        return

    cap = _lane_cap(_resolve_qbo_lane(lane))
    key = f"qbo:sem:{realm_id}"
    member = uuid.uuid4().hex
    deadline = time.monotonic() + QBO_SEMAPHORE_ACQUIRE_TIMEOUT
    acquired = False
    try:
        while True:
            now = time.time()
            try:
                # Drop expired holders, then try to claim a slot. ZSET score =
                # expiry epoch; a slot older than the TTL is reaped here.
                client.zremrangebyscore(key, 0, now)
                if client.zcard(key) < cap:
                    client.zadd(key, {member: now + QBO_SEMAPHORE_SLOT_TTL_SECONDS})
                    client.expire(key, QBO_SEMAPHORE_SLOT_TTL_SECONDS * 2)
                    acquired = True
                    break
            except Exception as exc:  # noqa: BLE001 — Redis hiccup must not block the call; degrade to no-limiter
                logger.warning("QBO semaphore acquire failed; proceeding without limiter: %s", redact_to_string(str(exc), max_length=200))
                yield
                return
            if time.monotonic() >= deadline:
                # Could not get a slot in time — proceed rather than fail the
                # sync; the per-request backoff (B3.1) still absorbs a 429 if the
                # realm is genuinely saturated.
                logger.warning("QBO semaphore wait timed out for realm=%s; proceeding (backoff still guards 429)", realm_id)
                break
            time.sleep(QBO_SEMAPHORE_POLL_SECONDS)
        yield
    finally:
        if acquired:
            with contextlib.suppress(Exception):
                client.zrem(key, member)


def qbo_sync_in_progress(realm_id: str) -> bool:
    """Non-mutating peek: is a ``sync_all_data`` lock currently held for this realm?

    Lets the enqueue path (the ``/all`` route) skip queuing a trigger that the
    task would no-op anyway. This does NOT acquire the lock — only the task does
    — so it never steals the slot. Degrades to ``False`` (allow the enqueue) when
    Redis is unavailable; the task's own acquire-or-skip is the real guard.
    """
    client = _qbo_redis()
    if client is None:
        return False
    try:
        return bool(client.exists(f"qbo:synclock:{realm_id}"))
    except Exception as exc:  # noqa: BLE001 — a peek failure must not block the enqueue; degrade to "not running"
        logger.warning("QBO sync-lock peek failed; allowing enqueue: %s", redact_to_string(str(exc), max_length=200))
        return False


@contextlib.contextmanager
def qbo_realm_sync_lock(realm_id: str, *, ttl_seconds: int = QBO_SYNC_LOCK_TTL_SECONDS) -> Iterator[bool]:
    """Per-realm exclusivity lock for ``sync_all_data`` (Redis SETNX + TTL).

    Yields ``True`` if THIS caller acquired the lock (it owns the sync), ``False``
    if another sync for the same realm already holds it (the caller should
    no-op/return early). The lock auto-expires after ``ttl_seconds`` so a crashed
    holder cannot wedge the realm forever, and is released in ``finally`` only by
    its owner (a value token guards against deleting a successor's lock). Degrades
    to "always acquired" when Redis is unavailable — the lock prevents wasted QBO
    load, not corruption (upserts are idempotent), so no-Redis simply forgoes the
    dedupe.
    """
    client = _qbo_redis()
    if client is None:
        yield True
        return

    key = f"qbo:synclock:{realm_id}"
    token = uuid.uuid4().hex
    try:
        got = client.set(key, token, nx=True, ex=ttl_seconds)
    except Exception as exc:  # noqa: BLE001 — a Redis failure degrades to no-lock, never crashes the sync
        logger.warning("QBO sync-lock acquire failed; proceeding without dedupe: %s", redact_to_string(str(exc), max_length=200))
        yield True
        return

    if not got:
        yield False
        return
    try:
        yield True
    finally:
        # Release only if we still own it (compare-and-delete) so we never delete
        # a successor's lock that took over after our TTL lapsed.
        with contextlib.suppress(Exception):
            release = (
                "if redis.call('get', KEYS[1]) == ARGV[1] then "
                "return redis.call('del', KEYS[1]) else return 0 end"
            )
            client.eval(release, 1, key, token)


# Intuit per-error "Message" values are generic categories ("Stale Object Error",
# "Duplicate Name Exists Error"); the per-error "Detail" field can carry tenant
# data (account/vendor/customer names, memos) and must never be logged or
# surfaced. Keep Message values short as defence-in-depth.
_FAULT_MESSAGE_MAX = 120


def parse_qbo_fault(resp: Any) -> Dict[str, Any]:
    """
    Extract log-safe Intuit fault metadata from an error response.

    Returns only non-sensitive fields: HTTP status, fault type, fault codes,
    generic fault Message categories, the intuit_tid correlation id, and a
    retryable flag. Never returns the response body or the per-error
    ``Detail`` field, which can contain tenant data.
    """
    status = getattr(resp, "status_code", None)
    headers = getattr(resp, "headers", None) or {}
    fault_info: Dict[str, Any] = {
        "status": status,
        "fault_type": None,
        "fault_codes": [],
        "fault_messages": [],
        "intuit_tid": headers.get("intuit_tid"),
        "retryable": bool(isinstance(status, int) and (status == 429 or status >= 500)),
    }
    try:
        parsed = resp.json()
    except Exception:
        return fault_info
    if not isinstance(parsed, dict):
        return fault_info

    fault = parsed.get("Fault") or parsed.get("fault") or {}
    if isinstance(fault, dict):
        fault_type = fault.get("type") or fault.get("Type")
        if fault_type:
            fault_info["fault_type"] = str(fault_type)[:60]
        errors = fault.get("Error") or fault.get("error") or []
        if isinstance(errors, dict):
            errors = [errors]
        if isinstance(errors, list):
            for err in errors:
                if not isinstance(err, dict):
                    continue
                code = err.get("code") or err.get("Code")
                if code:
                    fault_info["fault_codes"].append(str(code)[:20])
                message = err.get("Message") or err.get("message")
                if message:
                    fault_info["fault_messages"].append(
                        redact_to_string(str(message), max_length=_FAULT_MESSAGE_MAX)
                    )

    # OAuth-style errors from the Intuit token endpoint: {"error": "invalid_grant"}
    oauth_error = parsed.get("error")
    if oauth_error and not fault_info["fault_codes"] and isinstance(oauth_error, str):
        fault_info["fault_codes"].append(redact_to_string(oauth_error, max_length=40))

    return fault_info


def qbo_fault_log_summary(resp: Any) -> str:
    """Single log-safe string summarising a QBO error response (never the body)."""
    return json.dumps(parse_qbo_fault(resp), sort_keys=True, default=str)


def qbo_fault_public_message(fault: Dict[str, Any]) -> str:
    """User-facing error summary built only from fault codes + generic messages."""
    codes = ", ".join(fault.get("fault_codes") or []) or "unknown"
    messages = "; ".join(fault.get("fault_messages") or [])
    base = f"QuickBooks returned HTTP {fault.get('status')} (fault code {codes}"
    if messages:
        base += f": {messages}"
    return base + ")"


def _raise_qbo_provider_error(operation: str, exc: Exception) -> None:
    logger.warning("QBO provider operation failed: operation=%s error=%s", operation, redact_to_string(str(exc), max_length=300))
    raise HTTPException(
        status_code=502,
        detail={
            "error_code": "qbo_provider_error",
            "user_message": "QuickBooks did not return the requested financial data. Retry after the connection recovers.",
            "source": "quickbooks_online",
            "operation": operation,
            "retryable": True,
        },
    ) from exc


def _payload_shape(data: Any) -> str:
    if isinstance(data, dict):
        return json.dumps(
            {
                "keys": sorted(str(key) for key in data.keys())[:30],
                "line_count": len(data.get("Line", [])) if isinstance(data.get("Line"), list) else None,
            },
            sort_keys=True,
        )
    return type(data).__name__


def _basic_auth_header() -> str:
    """
    Basic auth header for Intuit token endpoint: base64(client_id:client_secret).
    """
    token = f"{settings.qbo_client_id}:{settings.qbo_client_secret}"
    b64 = base64.b64encode(token.encode()).decode()
    return f"Basic {b64}"


class QBOClient:
    """
    Thin wrapper around the QuickBooks Online Accounting API for a single company.
    """

    # PLAN-EXEMPT: chunk C6 (AC-7.1) of ratified plan 20260706-agent-path-remediation —
    # app/qbo_client.py is C6's declared write surface; active-plan surface_scope omits it.
    def __init__(
        self,
        access_token: str,
        realm_id: str,
        lane: "str | None" = None,
        *,
        on_auth_refresh: "Callable[[], str] | None" = None,
        on_auth_dead: "Callable[[str], None] | None" = None,
    ):
        # PLAN-EXEMPT: QBO connection-health revocation-detection — the 401 self-heal seam.
        self.access_token = access_token
        self.realm_id = realm_id
        # C6 lane hint for the per-realm semaphore: None keeps ambient resolution
        # (context override → Celery-task detection → interactive), so untouched
        # constructors change nothing; an explicit lane pins every call this
        # client makes to that lane.
        self.lane = lane
        # AC-1 seam (revocation-detection): the client is constructed decoupled from
        # the DB (access_token + realm only), so it cannot refresh itself. These
        # optional callbacks — wired by get_qbo_client_from_db, which HAS db + realm —
        # let a 401 force ONE refresh through the single-flight lock (on_auth_refresh)
        # and, if the grant is dead, surface the reconnect signal (on_auth_dead). A raw
        # QBOClient(...) built without them keeps today's behavior (raise 401).
        self._on_auth_refresh = on_auth_refresh
        self._on_auth_dead = on_auth_dead

        base_domain = (
            "sandbox-quickbooks.api.intuit.com"
            if settings.qbo_environment == "sandbox"
            else "quickbooks.api.intuit.com"
        )
        self.base_url = f"https://{base_domain}/v3/company/{self.realm_id}"

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _send_with_auth_retry(
        self,
        send_fn: "Callable[[], requests.Response]",
        *,
        retry_5xx: bool = True,
    ) -> "requests.Response":
        # PLAN-EXEMPT: QBO connection-health revocation-detection — 401 force-refresh-and-retry (AC-1).
        """Send a QBO call under the per-realm semaphore + 429/5xx backoff, then SELF-HEAL a 401.

        AC-1: a 401 means the stored access token is server-invalid REGARDLESS of its
        clock expiry (the "clock-fresh but server-invalid" state). If an ``on_auth_refresh``
        callback is wired, force EXACTLY ONE refresh through the single-flight lock and
        retry the SAME call once with the rotated token. If the refresh reports the grant
        dead it raises the reconnect signal (409) from inside the callback; if the retry
        STILL 401s, the grant is dead too → ``on_auth_dead`` flags it + raises the reconnect
        signal. A 401 is safe to retry even for a WRITE: it is rejected at auth BEFORE
        processing, so nothing mutated the GL (unlike a 5xx, which keeps retry_5xx=False).
        A client built WITHOUT the callbacks returns the 401 for the caller's legacy handling.
        """
        with qbo_realm_semaphore(self.realm_id, lane=self.lane):
            resp = _request_with_qbo_retry(send_fn, retry_5xx=retry_5xx)
            if resp.status_code != 401 or self._on_auth_refresh is None:
                return resp
            # Force one refresh (single-flight) and retry once with the rotated token.
            self.access_token = self._on_auth_refresh()
            resp = _request_with_qbo_retry(send_fn, retry_5xx=retry_5xx)
            if resp.status_code == 401 and self._on_auth_dead is not None:
                self._on_auth_dead("retry_still_401")  # raises the reconnect signal
            return resp

    def get_company_info(self) -> Dict[str, Any]:
        """
        Fetch high-level company info, including CompanyName.
        """
        url = f"{self.base_url}/companyinfo/{self.realm_id}"
        # PLAN-EXEMPT: AP-QBO-01 minorversion pin on get_company_info() — same surgical follow-up;
        # app/qbo_client.py is outside plan 20260625-intent-routing-receivables's surface_scope. Pin
        # =73 (matches get_report() / get_general_ledger()) so the company-info shape stays stable.
        params = {"minorversion": "73"}
        # B3.2 semaphore + B3.1 429/5xx backoff + AC-1 401 self-heal (revocation-detection).
        resp = self._send_with_auth_retry(
            lambda: requests.get(url, headers=self._headers(), params=params, timeout=REQUEST_TIMEOUT_SECONDS)  # FASTAPI-OK: in-method request call (QBOClient.get_company_info), not module-import-time I/O  # PLAN-EXEMPT: QBO connection-health 401 self-heal seam  # QBO-OK: minorversion-pinned-upstream
        )
        if resp.status_code == 401:
            raise HTTPException(status_code=401, detail="QBO Access Token expired/invalid")
        resp.raise_for_status()
        return resp.json()

    def query(self, query: str) -> Dict[str, Any]:
        """
        Run a QuickBooks SQL-like query, e.g.:

            SELECT * FROM Invoice STARTPOSITION 1 MAXRESULTS 50
        """
        url = f"{self.base_url}/query"
        # PLAN-EXEMPT: AP-QBO-01 minorversion pin on query() — surgical follow-up surfaced by plan
        # 20260625-intent-routing-receivables (Deliverable C's search-index path calls client.query);
        # app/qbo_client.py is outside that plan's surface_scope. Pin =73 to match get_report() /
        # get_general_ledger() so the response shape stays stable under the shared parsers.
        params = {"minorversion": "73", "query": query}
        # B3.2 per-realm concurrency gate + B3.1 per-request 429/5xx backoff.
        resp = self._send_with_auth_retry(
            lambda: requests.get(url, headers=self._headers(), params=params, timeout=REQUEST_TIMEOUT_SECONDS)  # FASTAPI-OK: in-method request call (QBOClient.query), not module-import-time I/O  # PLAN-EXEMPT: QBO connection-health 401 self-heal seam  # QBO-OK: minorversion-pinned-upstream
        )
        if resp.status_code == 401:
            raise HTTPException(status_code=401, detail="QBO Access Token expired/invalid")
        resp.raise_for_status()
        return resp.json()

    def get_report(
        self,
        report_name: str,
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Call a QuickBooks Online report endpoint, e.g. ProfitAndLoss.

        Example:
            client.get_report(
                "ProfitAndLoss",
                {"date_macro": "ThisFiscalYearToDate", "summarize_column_by": "Month"}
            )
        """
        url = f"{self.base_url}/reports/{report_name}"
        # Pin the rolling Intuit minorversion so the shared report parsers — including
        # the Aged AR/AP bucket matcher in app/analysis/ar_aging.py & ap_aging.py — see a
        # stable column shape. An unpinned call rides Intuit's advancing default and drifts
        # the response shape under the parsers. Matches the GeneralLedger pin (minorversion=73)
        # in get_general_ledger() below. Caller-supplied params win on collision.
        merged_params = {"minorversion": "73", **(params or {})}
        # B3.2 per-realm concurrency gate + B3.1 per-request 429/5xx backoff.
        resp = self._send_with_auth_retry(
            lambda: requests.get(url, headers=self._headers(), params=merged_params, timeout=REQUEST_TIMEOUT_SECONDS)  # FASTAPI-OK: in-method request call, not module-import-time I/O (inside QBOClient.get_report)  # PLAN-EXEMPT: QBO connection-health 401 self-heal seam  # QBO-OK: minorversion-pinned-upstream
        )

        if resp.status_code == 401:
            raise HTTPException(status_code=401, detail="QBO Access Token expired/invalid")
            
        try:
            resp.raise_for_status()
        except Exception:
            logger.warning(
                "QBO report request failed: report=%s status=%s fault=%s",
                report_name,
                resp.status_code,
                qbo_fault_log_summary(resp),
            )
            raise
        return resp.json()

    def create_journal_entry(self, entry_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Create a Journal Entry in QBO.
        entry_data should match the QBO JournalEntry structure.
        """
        url = f"{self.base_url}/journalentry"
        # QBO-OK: balanced-upstream — the debit==credit assert is in the JE builder
        # (app/lbo/journal_entries.py) before entry_data reaches this client.
        # QBO-OK: idempotent-upstream — the SHA-256 QBOActionExecution ledger in the
        # action executor dedupes the JE intent before this POST is reached.
        # B3.2 per-realm concurrency gate + B3.1 backoff. retry_5xx=False on a
        # WRITE: a 429 (rate-limited, never processed) is safe to repeat, but a
        # 5xx that may have already booked the JE is NOT silently re-POSTed.
        resp = self._send_with_auth_retry(
            lambda: requests.post(url, headers=self._headers(), json=entry_data, timeout=REQUEST_TIMEOUT_SECONDS),  # FASTAPI-OK: in-method request call (QBOClient.create_journal_entry), not module-import-time I/O  # PLAN-EXEMPT: QBO connection-health 401 self-heal seam
            retry_5xx=False,
        )
        if resp.status_code == 401:
            raise HTTPException(status_code=401, detail="QBO Access Token expired/invalid")
        
        try:
            resp.raise_for_status()
        except Exception:
            logger.warning(
                "QBO journal entry create failed: status=%s fault=%s",
                resp.status_code,
                qbo_fault_log_summary(resp),
            )
            raise

        return resp.json()

    # PLAN-EXEMPT: ratified plan 20260713-wave2-1-ar-collections C1 (marker
    # RATIFICATION-2026-07-13.md) — the four AR write methods (AC-1, decisions 2/4).
    def _post_ar_write(
        self,
        url: str,
        *,
        body: "dict | None",
        label: str,
        headers: "dict | None" = None,
        params: "dict | None" = None,
    ) -> dict:
        """POST an AR write with TYPED-UNCERTAIN semantics (decision 4 / E2).

        DELIBERATE, DOCUMENTED DIVERGENCE from ``create_journal_entry`` above:
        that method lets a 5xx flow to ``raise_for_status``, whose plain
        ``HTTPError`` the action executor classifies ``retryable_failed`` — and
        the retryable lane BLIND re-invokes with NO reconcile probe
        (``executor.py``: probe only on ``uncertain``). For a customer-facing
        create/send, a blind re-invoke is a duplicate invoice or a duplicate
        email. So here a provider 5xx and a network failure AFTER dispatch raise
        ``QBOUncertainOutcomeError`` BEFORE ``raise_for_status`` — the executor
        lands the ledger row ``uncertain`` and the retry path reconciles against
        QBO before any re-POST. A 4xx stays a confirmed reject (``HTTPError``).
        ``retry_5xx=False``: a 429 (rate-limited, never processed) is safely
        repeated by the shared backoff; a 5xx is never silently re-POSTed.
        """
        send_kwargs: dict = {
            "headers": headers or self._headers(),
            "timeout": REQUEST_TIMEOUT_SECONDS,
        }
        if body is not None:
            send_kwargs["json"] = body
        if params is not None:
            send_kwargs["params"] = params
        try:
            resp = self._send_with_auth_retry(
                lambda: requests.post(url, **send_kwargs),  # FASTAPI-OK: in-method request call (QBOClient._post_ar_write), not module-import-time I/O  # PLAN-EXEMPT: QBO connection-health 401 self-heal seam  # QBO-OK: minorversion-pinned-upstream — every caller embeds minorversion=73 in the URL
                retry_5xx=False,
            )
        except requests.RequestException as exc:
            raise QBOUncertainOutcomeError(f"QuickBooks {label} outcome unknown: the request may have reached QuickBooks before the connection failed. Do not blindly retry — reconcile first.") from exc
        if resp.status_code == 401:
            raise HTTPException(status_code=401, detail="QBO Access Token expired/invalid")
        if resp.status_code >= 500:
            # Never log the payload/body — parsed fault metadata only (AP-QBO-05).
            logger.error(
                "QBO %s returned 5xx (outcome ambiguous): realm_id=%s status=%s fault=%s",
                label,
                self.realm_id,
                resp.status_code,
                qbo_fault_log_summary(resp),
            )
            raise QBOUncertainOutcomeError(
                f"QuickBooks returned HTTP {resp.status_code} for the {label} — the write may "
                "or may not have been applied. Do not blindly retry — reconcile first."
            )
        try:
            resp.raise_for_status()
        except Exception:
            logger.warning(
                "QBO %s rejected: realm_id=%s status=%s fault=%s",
                label,
                self.realm_id,
                resp.status_code,
                qbo_fault_log_summary(resp),
            )
            raise
        return resp.json()

    def create_invoice(self, data: dict) -> dict:
        """Create an Invoice (minorversion=73). Typed-uncertain write — see _post_ar_write."""
        # QBO-OK: idempotent-upstream — the SHA-256 QBOActionExecution ledger in the
        # action executor dedupes the intent before this POST is reached.
        return self._post_ar_write(f"{self.base_url}/invoice?minorversion=73", body=data, label="Invoice create")

    def create_payment(self, data: dict) -> dict:
        """Create a Payment (minorversion=73). Typed-uncertain write — see _post_ar_write."""
        # QBO-OK: idempotent-upstream — executor SHA-256 ledger dedupes upstream.
        return self._post_ar_write(f"{self.base_url}/payment?minorversion=73", body=data, label="Payment create")

    def create_credit_memo(self, data: dict) -> dict:
        """Create a CreditMemo (minorversion=73). Typed-uncertain write — see _post_ar_write."""
        # QBO-OK: idempotent-upstream — executor SHA-256 ledger dedupes upstream.
        return self._post_ar_write(f"{self.base_url}/creditmemo?minorversion=73", body=data, label="CreditMemo create")

    # PLAN-EXEMPT: ratified plan 20260713-wave2-2-ap-batches chunk A (marker
    # RATIFICATION-2026-07-13.md) — decision 1: the P0 phantom-create fix. The human AP
    # lane called qbo.create("BillPayment", ...) which does not exist on QBOClient —
    # bills have never been paid in prod. This is the REAL method, riding the
    # typed-uncertain write seam.
    def create_bill_payment(self, data: dict) -> dict:
        """Create a BillPayment (minorversion=73). Typed-uncertain write — see _post_ar_write.

        A provider 5xx / network failure after dispatch raises
        ``QBOUncertainOutcomeError`` BEFORE raise_for_status (the payment may
        already exist in QBO — reconcile before any re-POST); a 4xx stays a
        confirmed reject. Real money: never blindly retried.
        """
        # QBO-OK: idempotent-upstream — the BillPaymentExecution claim ledger
        # (bill_payment_core.submit_bill_payment_batch) dedupes per-bill upstream.
        return self._post_ar_write(f"{self.base_url}/billpayment?minorversion=73", body=data, label="BillPayment create")

    def send_invoice(self, invoice_id: str, send_to: "str | None" = None) -> dict:
        """Send an invoice via QBO's native delivery (decisions 1/2).

        ``sendTo`` is OMITTED by default — QBO delivers to the invoice's own
        ``BillEmail`` from the company's configured address. The optional
        ``send_to`` parameter exists for FUTURE BACKEND-VALIDATED use ONLY and
        is never populated from model input (a model-suppliable recipient is a
        prompt-injection exfiltration lane for invoice PDFs — decision 2).
        Typed-uncertain write — see _post_ar_write.
        """
        url = f"{self.base_url}/invoice/{invoice_id}/send?minorversion=73"
        # Intuit's send operation takes no JSON body and expects octet-stream.
        headers = {**self._headers(), "Content-Type": "application/octet-stream"}
        params = {"sendTo": send_to} if send_to else None
        # QBO-OK: idempotent-upstream — executor SHA-256 ledger + the decision-7
        # per-invoice send throttle dedupe upstream.
        return self._post_ar_write(url, body=None, label="Invoice send", headers=headers, params=params)

    def get_transaction(self, entity: str, txn_id: str) -> Dict[str, Any]:
        """
        Fetch a single transaction by ID. Useful for getting SyncToken.
        """
        url = f"{self.base_url}/{entity.lower()}/{txn_id}"
        # PLAN-EXEMPT: AP-QBO-01 minorversion pin on get_transaction() — same surgical follow-up;
        # app/qbo_client.py is outside plan 20260625-intent-routing-receivables's surface_scope. Pin
        # =73 (matches get_report() / get_general_ledger()) so the single-entity GET (SyncToken reads
        # + the search-index single-entity fetch) sees a stable shape.
        params = {"minorversion": "73"}
        # B3.2 per-realm concurrency gate + B3.1 per-request 429/5xx backoff.
        resp = self._send_with_auth_retry(
            lambda: requests.get(url, headers=self._headers(), params=params, timeout=REQUEST_TIMEOUT_SECONDS)  # FASTAPI-OK: in-method request call (QBOClient.get_transaction), not module-import-time I/O  # PLAN-EXEMPT: QBO connection-health 401 self-heal seam  # QBO-OK: minorversion-pinned-upstream
        )
        if resp.status_code == 401:
            raise HTTPException(status_code=401, detail="QBO Access Token expired/invalid")
        resp.raise_for_status()
        return resp.json()

    def update_transaction(self, entity: str, txn_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Update an existing transaction (Invoice, Bill, Payment, etc.).
        Requires SyncToken to be up to date (usually passed in data).
        """
        # API requires operation=update qparam for full updates, 
        # or sparse=true can be used. Generally we assume 'data' is the payload.
        url = f"{self.base_url}/{entity.lower()}?operation=update"
        
        # Ensure Id is in the body, just in case
        data["Id"] = txn_id

        # QBO-OK: balanced-upstream — generic entity update; any JE balance is
        # asserted by the JE builder before the payload reaches this client.
        # QBO-OK: idempotent-upstream — the SyncToken in `data` makes the update a
        # compare-and-set (a stale-token retry is rejected by QBO, not re-applied).
        # B3.2 per-realm concurrency gate + B3.1 backoff. retry_5xx=False on a
        # WRITE: only a 429 (never processed) is repeated, never a maybe-applied 5xx.
        resp = self._send_with_auth_retry(
            lambda: requests.post(url, headers=self._headers(), json=data, timeout=REQUEST_TIMEOUT_SECONDS),  # FASTAPI-OK: in-method request call (QBOClient.update_transaction), not module-import-time I/O  # PLAN-EXEMPT: QBO connection-health 401 self-heal seam
            retry_5xx=False,
        )
        if resp.status_code == 401:
             raise HTTPException(status_code=401, detail="QBO Access Token expired/invalid")
             
        try:
            resp.raise_for_status()
        except Exception:
            logger.warning(
                "QBO transaction update failed: entity=%s status=%s fault=%s",
                entity,
                resp.status_code,
                qbo_fault_log_summary(resp),
            )
            raise
             
        return resp.json()

    def void_transaction(self, entity: str, txn_id: str, sync_token: str) -> Dict[str, Any]:
        """
        Void a transaction.
        """
        url = f"{self.base_url}/{entity.lower()}?operation=void"
        payload = {
            "Id": txn_id,
            "SyncToken": sync_token
        }

        # QBO-OK: balanced-upstream — a void carries no debit/credit lines to balance.
        # QBO-OK: idempotent-upstream — the SyncToken makes the void a compare-and-set;
        # a stale-token retry of an already-voided txn is rejected by QBO, not re-applied.
        # B3.2 per-realm concurrency gate + B3.1 backoff. retry_5xx=False on a
        # WRITE: only a 429 (never processed) is repeated, never a maybe-applied 5xx.
        resp = self._send_with_auth_retry(
            lambda: requests.post(url, headers=self._headers(), json=payload, timeout=REQUEST_TIMEOUT_SECONDS),  # FASTAPI-OK: in-method request call (QBOClient.void_transaction), not module-import-time I/O  # PLAN-EXEMPT: QBO connection-health 401 self-heal seam
            retry_5xx=False,
        )
        if resp.status_code == 401:
             raise HTTPException(status_code=401, detail="QBO Access Token expired/invalid")
             
        try:
            resp.raise_for_status()
        except Exception:
            logger.warning(
                "QBO transaction void failed: entity=%s status=%s fault=%s",
                entity,
                resp.status_code,
                qbo_fault_log_summary(resp),
            )
            raise
             
        return resp.json()

    def get_bank_accounts(self) -> list:
        """
        Returns all Bank and Credit Card accounts from QBO with their current balances.
        """
        query = (
            "SELECT * FROM Account WHERE AccountType IN ('Bank', 'Credit Card') "
            "AND Active = true MAXRESULTS 200"
        )
        resp = self.query(query)
        return resp.get("QueryResponse", {}).get("Account", [])

    def get_bank_feed(self, max_results: int = 200, days_back: int = 90) -> list:
        """
        Fetch recent bank-linked transactions from QBO that are candidates for
        the 'For Review' feed.

        NOTE: The QBO V3 API does NOT expose the raw 'For Review' bank feed
        (BankTransaction is not a queryable entity). Instead we replicate the
        same data by pulling:
          - Purchase entities  (debits / expenses from bank/CC accounts)
          - Deposit entities   (credits / income into bank accounts)

        Returns a normalised list of dicts with fields matching what the
        bank_feed service expects.
        """
        from datetime import date, timedelta
        start_date = (date.today() - timedelta(days=days_back)).isoformat()

        results: list = []

        # ── Pull Purchase (debits / expenses) ─────────────────────────────
        try:
            purchase_q = (
                f"SELECT * FROM Purchase WHERE TxnDate >= '{start_date}' "
                f"MAXRESULTS {max_results}"
            )
            resp = self.query(purchase_q)
            purchases = resp.get("QueryResponse", {}).get("Purchase", [])
            for p in purchases:
                account_ref = p.get("AccountRef", {})
                lines = p.get("Line", [])
                memo = p.get("PrivateNote") or ""
                desc = memo
                line_account_ref = {}
                if lines:
                    first_line = lines[0]
                    detail = (
                        first_line.get("AccountBasedExpenseLineDetail")
                        or first_line.get("ItemBasedExpenseLineDetail")
                        or {}
                    )
                    line_account_ref = detail.get("AccountRef", {})
                    if not desc:
                        desc = first_line.get("Description", "")

                results.append({
                    "Id": f"purchase-{p['Id']}",
                    "_qbo_id": p["Id"],
                    "_entity_type": "Purchase",
                    "TxnDate": p.get("TxnDate", ""),
                    "Amount": p.get("TotalAmt", 0),
                    "TransactionType": "DEBIT",
                    "Description": desc or p.get("DocNumber", ""),
                    "BankAccountRef": account_ref,
                    "LineAccountRef": line_account_ref,
                    "EntityRef": p.get("EntityRef", {}),
                })
        except Exception as e:
            _raise_qbo_provider_error("purchase_bank_feed_query", e)

        # ── Pull Deposit (credits / income) ───────────────────────────────
        try:
            deposit_q = (
                f"SELECT * FROM Deposit WHERE TxnDate >= '{start_date}' "
                f"MAXRESULTS {max_results}"
            )
            resp = self.query(deposit_q)
            deposits = resp.get("QueryResponse", {}).get("Deposit", [])
            for d in deposits:
                account_ref = d.get("DepositToAccountRef", {})
                lines = d.get("Line", [])
                desc = d.get("PrivateNote") or ""
                line_account_ref = {}
                entity_ref = {}
                if lines:
                    first_line = lines[0]
                    detail = first_line.get("DepositLineDetail", {})
                    line_account_ref = detail.get("AccountRef", {})
                    entity_ref = detail.get("Entity", {}).get("EntityRef", {})
                    if not desc:
                        desc = first_line.get("Description", "")

                results.append({
                    "Id": f"deposit-{d['Id']}",
                    "_qbo_id": d["Id"],
                    "_entity_type": "Deposit",
                    "TxnDate": d.get("TxnDate", ""),
                    "Amount": abs(float(d.get("TotalAmt", 0))),
                    "TransactionType": "CREDIT",
                    "Description": desc or "",
                    "BankAccountRef": account_ref,
                    "LineAccountRef": line_account_ref,
                    "EntityRef": entity_ref,
                })
        except Exception as e:
            _raise_qbo_provider_error("deposit_bank_feed_query", e)

        return results


    def get_general_ledger(self, months_back: int = 6) -> dict:
        """
        Fetch the QBO GeneralLedger report for the past N months.
        Returns the raw report dict (Columns + Rows structure).
        """
        from datetime import date, timedelta
        end_date = date.today()
        start_date = end_date - timedelta(days=months_back * 30)

        url = f"{self.base_url.replace('/query', '')}/reports/GeneralLedger"
        params = {
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "accounting_method": "Accrual",
            "minorversion": "73",
        }
        # B3.2 per-realm concurrency gate + B3.1 per-request 429/5xx backoff.
        resp = self._send_with_auth_retry(
            lambda: requests.get(url, headers=self._headers(), params=params, timeout=REQUEST_TIMEOUT_SECONDS)  # FASTAPI-OK: in-method request call (QBOClient.get_general_ledger), not module-import-time I/O  # PLAN-EXEMPT: QBO connection-health 401 self-heal seam  # QBO-OK: minorversion-pinned-upstream
        )
        if resp.status_code == 401:
            raise HTTPException(status_code=401, detail="QBO token expired")
        try:
            resp.raise_for_status()
            return resp.json().get("QueryResponse", resp.json())
        except Exception as e:
            logger.warning(
                "GL report fetch failed: error=%s fault=%s",
                redact_to_string(str(e), max_length=300),
                qbo_fault_log_summary(resp),
            )
            raise HTTPException(
                status_code=502,
                detail={
                    "error_code": "qbo_provider_error",
                    "user_message": "QuickBooks did not return the General Ledger report. Retry after the connection recovers.",
                    "source": "quickbooks_online",
                    "operation": "general_ledger_report",
                    "retryable": True,
                },
            ) from e

# ----------------------------------------------------------------------
# TOKEN REFRESH UTILITIES
# ----------------------------------------------------------------------
# PLAN-EXEMPT: chunk C6 (AC-7.2) of ratified plan 20260706-agent-path-remediation —
# app/qbo_client.py is C6's declared write surface; active-plan surface_scope omits it.
# C6 — bound the WAITER side of the refresh single-flight. The refresher holds
# the FOR-UPDATE row lock across a ≤30s Intuit POST; without a lock timeout each
# waiter blocks unbounded on that lock while pinning a DB pool connection. The
# waiter now takes the lock under a transaction-scoped ``lock_timeout`` and, on
# timeout, re-reads the (likely already rotated) row instead of blocking —
# exactly ONE refresher ever POSTs (AP-QBO-02 preserved).
QBO_REFRESH_LOCK_TIMEOUT_MS = 5000        # per-attempt FOR-UPDATE wait ceiling
QBO_REFRESH_WAIT_MAX_SECONDS = 45.0       # total waiter budget: winner's ≤30s POST + commit headroom
QBO_REFRESH_WAIT_POLL_SECONDS = 0.25      # pause between bounded lock attempts


def _lock_token_row_bounded(db: Session, token: QBOToken) -> bool:
    """Try to take the single-flight row lock on ``token``, bounded.

    Postgres: applies a transaction-scoped ``lock_timeout`` (``set_config(...,
    is_local => true)`` — the parameterizable SET LOCAL form) then re-reads the
    row FOR UPDATE. A lock timeout (SQLSTATE 55P03) rolls the transaction back
    and returns False so the caller can re-read the rotated token instead of
    blocking unbounded on a pooled connection. Any other error propagates.

    Non-Postgres (the SQLite test lane): FOR UPDATE is a no-op there, so the
    plain locked re-read "acquires" immediately — the double-checked staleness
    guard in ``_refresh_qbo_token`` keeps a redundant refresh harmless, exactly
    today's behavior.
    """
    review = _qbo_review_budget.get()
    if review is not None:
        review[0].remaining()
    bind = db.get_bind()
    if bind is None or bind.dialect.name != "postgresql":
        db.refresh(token, with_for_update=True)
        if review is not None:
            review[0].remaining()
        return True
    try:
        # SET LOCAL: scoped to this transaction only — it can never leak onto a
        # pooled connection (the commit/rollback that ends the txn clears it).
        db.execute(
            sa_text("SELECT set_config('lock_timeout', :ms, true)"),
            {"ms": str(QBO_REFRESH_LOCK_TIMEOUT_MS if review is None else max(1, min(QBO_REFRESH_LOCK_TIMEOUT_MS, int(review[0].remaining() * 1000))))},
        )
        db.refresh(token, with_for_update=True)
        if review is not None:
            review[0].remaining()
        return True
    except OperationalError as exc:
        db.rollback()
        pgcode = getattr(getattr(exc, "orig", None), "pgcode", None)
        if pgcode == "55P03" or "lock timeout" in str(exc).lower():
            return False
        raise


# PLAN-EXEMPT: QBO connection-health revocation-detection — honest dead-grant state (AC-2/AC-3).
# Short, log-safe health-error CATEGORIES stored in qbo_tokens.last_health_error —
# NEVER a response body or token material (AP-QBO-05 never-log-the-payload).
QBO_HEALTH_INVALID_GRANT = "invalid_grant"
QBO_HEALTH_REFRESH_TOKEN_EXPIRED = "refresh_token_expired"
QBO_HEALTH_REFRESH_NO_TOKENS = "refresh_no_tokens"
QBO_HEALTH_RETRY_STILL_401 = "retry_still_401"


def _qbo_refresh_unavailable_exc() -> HTTPException:
    # PLAN-EXEMPT: QBO connection-health revocation-detection — transient-refresh 503 helper.
    """A retryable 503 for a TRANSIENT refresh failure (network / 5xx) — NOT a dead grant.

    Factored out so the raise stays a single line (``raise ... from exc``), keeping the
    cause chain on the network path without a multi-line raise inside the except block.
    """
    return HTTPException(
        status_code=503,
        detail={
            "error_code": "qbo_token_refresh_unavailable",
            "user_message": "QuickBooks couldn't refresh the connection right now. Retry shortly.",
            "source": "quickbooks_online",
            "retryable": True,
        },
    )


def _mark_token_reconnect_required(db: Session, token: QBOToken, category: str) -> None:
    # PLAN-EXEMPT: QBO connection-health revocation-detection — the honest dead-grant transition.
    """Flag a QBOToken as needing a user reconnect after a DEFINITIVE auth failure.

    AC-2 + AC-3: a dead grant must NOT keep a phantom future ``access_expires_at`` — that
    is the "clock-fresh but server-invalid" state that makes the refresh path refuse to run
    (the clock lies). So: clear the expiry to NOW (honest — the access token IS dead; the
    column is NOT NULL, so "expired now" is the truthful clearing), set ``reconnect_required``
    + the health metadata (a SHORT category only — never a body/token), and record a
    ``qbo_token_revoked`` auth event. Idempotent: re-flagging just refreshes the timestamp.
    """
    now = datetime.utcnow()
    token.reconnect_required = True
    token.health_checked_at = now
    token.last_health_error = category
    token.access_expires_at = now
    db.commit()  # QBO-OK: not a token refresh — persists the dead-grant flag, no rotation here
    db.refresh(token)
    try:
        from .services.auth_migration import record_auth_event
        org = organization_for_company(db, token.realm_id)
        record_auth_event(
            db,
            event_type="qbo_token_revoked",
            organization=org,
            event_status="failed",
            details={"realm_id": token.realm_id, "reason": category, "detected_by": "auto_health"},
        )
        db.commit()  # QBO-OK: audit-event persist only; the reconnect flag is already committed above
    except Exception as exc:  # noqa: BLE001 — the flag is the load-bearing state; the audit row is best-effort
        logger.warning(
            "Failed to record qbo_token_revoked auth event for realm_id=%s: %s",
            token.realm_id, redact_to_string(str(exc), max_length=200),
        )


def _refresh_is_unnecessary(
    token: QBOToken, now: datetime, force: bool, expected_access: "str | None"
) -> bool:
    # PLAN-EXEMPT: QBO connection-health revocation-detection — the force-aware double-check.
    """True when the refresh should be SKIPPED (a concurrent flight already handled it).

    The clock path (force=False) keys on expiry — UNCHANGED behavior. The FORCED path
    (force=True, a 401-triggered refresh) CANNOT trust the clock (the token is invalid
    regardless of its expiry), so it keys on ROTATION: if the stored ciphertext no longer
    equals the ``expected_access`` the caller was using, a concurrent flight already
    rotated it and this flight must NOT re-POST — the single-flight invariant holds.
    """
    if force:
        return expected_access is not None and token.access_token != expected_access
    return bool(token.access_expires_at and token.access_expires_at > now + timedelta(minutes=5))


def _refresh_qbo_token(
    db: Session, token: QBOToken, *, force: bool = False, expected_access: "str | None" = None
) -> QBOToken:
    # PLAN-EXEMPT: QBO connection-health revocation-detection — force refresh + honest failure (AC-1/2/3).
    """
    Refresh a QBO access token using its refresh_token; persist + return the updated row.

    SINGLE-FLIGHT + ATOMIC (AP-QBO-02) — UNCHANGED. Intuit ROTATES the refresh token on
    every refresh and force-expires the prior one; two concurrent refreshes for one realm
    would both POST the same stale refresh_token and clobber the winner's rotation. We
    serialize per-connection with a bounded ``SELECT ... FOR UPDATE`` row lock
    (``_lock_token_row_bounded``), double-check under the lock, and store the rotated
    (access, refresh, expiries) tuple in ONE commit which releases the lock. A waiter that
    times out on the lock re-reads the committed rotation and returns it without POSTing —
    a refresh only ever happens UNDER the lock, so single-flight is intact.

    FORCE (revocation-detection, AC-1/2/3): ``force=True`` is the 401-triggered path — the
    stored token is server-invalid REGARDLESS of its clock expiry, so the double-check keys
    on token ROTATION (``expected_access``) instead of the clock (``_refresh_is_unnecessary``):
    a waiter/flight that sees the winner's rotated ciphertext, or a ``reconnect_required``
    flag, returns/raises without a second POST — single-flight still holds. Every DEFINITIVE
    failure (invalid_grant / 4xx / no-tokens / refresh-token-expired) routes through
    ``_mark_token_reconnect_required`` (clears the stale expiry, flags the grant, records the
    event) and raises the 409 reconnect signal — it NEVER persists a success-shaped expiry
    (AC-3). A TRANSIENT failure (network / 5xx) raises a retryable 503 and leaves the row
    untouched. A SUCCESSFUL refresh clears any prior dead-state flag (self-heal).
    """
    # Single-flight: lock this realm's row and re-read it under the lock, bounded.
    review = _qbo_review_budget.get()
    deadline = time.monotonic() + (QBO_REFRESH_WAIT_MAX_SECONDS if review is None else min(QBO_REFRESH_WAIT_MAX_SECONDS, review[0].remaining()))
    while not _lock_token_row_bounded(db, token):
        # Lock timed out — a refresher is mid-flight. Re-read WITHOUT the lock; a
        # committed rotation / flag is visible to this fresh transaction.
        db.refresh(token)
        if review is not None:
            review[0].remaining()
        check_now = datetime.utcnow()
        if token.reconnect_required:
            # A concurrent flight already confirmed the grant dead — never re-POST it.
            raise qbo_reconnect_required_exception()
        if _refresh_is_unnecessary(token, check_now, force, expected_access):
            return token  # the winner already rotated it — no second refresh
        if time.monotonic() >= deadline:
            raise HTTPException(
                status_code=503,
                detail={
                    "error_code": "qbo_token_refresh_in_progress",
                    "user_message": "QuickBooks is refreshing its connection. Retry in a few seconds.",
                    "source": "quickbooks_online",
                    "retryable": True,
                },
            )
        # Bounded condition-poll (explicit deadline above) — never an unbounded wait.
        if review is None:
            time.sleep(QBO_REFRESH_WAIT_POLL_SECONDS)
        else:
            review[0].wait(QBO_REFRESH_WAIT_POLL_SECONDS)

    if review is not None:
        review[0].remaining()
    now = datetime.utcnow()

    # Double-checked UNDER the lock (same predicate as the wait loop).
    if token.reconnect_required:
        raise qbo_reconnect_required_exception()
    if _refresh_is_unnecessary(token, now, force, expected_access):
        return token

    # The refresh token itself has expired → the grant is dead → honest reconnect state.
    if token.refresh_expires_at and token.refresh_expires_at <= now:
        if review is not None:
            review[0].remaining()
        _mark_token_reconnect_required(db, token, QBO_HEALTH_REFRESH_TOKEN_EXPIRED)
        if review is not None:
            review[0].remaining()
        raise qbo_reconnect_required_exception()

    headers = {
        "Authorization": _basic_auth_header(),
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    data = {
        "grant_type": "refresh_token",
        "refresh_token": decrypt_secret(token.refresh_token),
    }

    try:
        if review is None:
            resp = requests.post(  # FASTAPI-OK: existing credential refresh path
                settings.intuit_token_endpoint,
                headers=headers,
                data=data,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        else:
            resp = _review_refresh_post(headers, data, review[0], review[1])
    except requests.RequestException as exc:
        # TRANSIENT network failure: the grant may be perfectly fine. Do NOT flag reconnect
        # and NEVER persist a fresh expiry. Never log the exception — its repr can embed the
        # token form body (see qbo_token_revoke.py). 503, retryable.
        logger.warning("QBO token refresh network error: realm_id=%s", token.realm_id)
        raise _qbo_refresh_unavailable_exc() from exc

    if review is not None:
        review[0].remaining()
    if not resp.ok:
        fault = parse_qbo_fault(resp)
        if review is not None:
            review[0].remaining()
        logger.warning(
            "QBO token refresh failed: realm_id=%s status=%s fault=%s",
            token.realm_id, resp.status_code, qbo_fault_log_summary(resp),
        )
        codes = fault.get("fault_codes") or []
        is_invalid_grant = any("invalid_grant" in str(code) for code in codes)
        status_code = resp.status_code if isinstance(resp.status_code, int) else 0
        # DEFINITIVE dead grant: invalid_grant, or ANY 4xx from the token endpoint (a
        # rejected refresh token). Flag reconnect (AC-2) + surface the 409 signal (AC-4).
        if is_invalid_grant or 400 <= status_code < 500:
            if review is not None:
                review[0].remaining()
            _mark_token_reconnect_required(
                db, token, QBO_HEALTH_INVALID_GRANT if is_invalid_grant else f"http_{status_code}"
            )
            if review is not None:
                review[0].remaining()
            raise qbo_reconnect_required_exception()
        # TRANSIENT (5xx): do NOT flag; no fresh expiry persisted (AC-3); retryable.
        raise _qbo_refresh_unavailable_exc()

    if review is not None:
        review[0].remaining()
    token_data = resp.json()
    if review is not None:
        review[0].remaining()

    new_access_token = token_data.get("access_token")
    new_refresh_token = token_data.get("refresh_token")
    expires_in = token_data.get("expires_in") or 3600
    refresh_expires_in = token_data.get("x_refresh_token_expires_in") or 86400

    if not new_access_token or not new_refresh_token:
        # A 200 with no tokens is a broken grant response → treat as dead, honestly (AC-3).
        if review is not None:
            review[0].remaining()
        _mark_token_reconnect_required(db, token, QBO_HEALTH_REFRESH_NO_TOKENS)
        if review is not None:
            review[0].remaining()
        raise qbo_reconnect_required_exception()

    # ATOMIC (AP-QBO-02): the rotated refresh token is persisted in the SAME commit as the
    # access token. A successful refresh also proves the grant is ALIVE → self-heal any
    # prior dead-state flag (revocation-detection).
    if review is not None:
        review[0].remaining()
    token.access_token = encrypt_secret(new_access_token)
    token.refresh_token = encrypt_secret(new_refresh_token)
    token.access_expires_at = now + timedelta(seconds=expires_in)
    token.refresh_expires_at = now + timedelta(seconds=refresh_expires_in)
    token.reconnect_required = False
    token.last_health_error = None
    token.health_checked_at = now

    if review is not None:
        review[0].remaining()
    db.commit()  # QBO-OK: refresh-is-single-flight-and-atomic — rotated refresh+access in ONE commit
    if review is not None:
        review[0].remaining()
    db.refresh(token)
    if review is not None:
        review[0].remaining()

    return token


def _assert_and_persist_qbo_token_scope(db: Session, token: QBOToken) -> None:
    original_scope = (token.organization_id, token.organization_company_id)
    assert_qbo_token_company_scope(db, token)
    if original_scope != (token.organization_id, token.organization_company_id):
        db.commit()
        db.refresh(token)


def _qbo_token_scope(db: Session, realm_id: str):
    # PLAN-EXEMPT: QBO connection-health revocation-detection — shared org-resolution scope.
    """The RLS scope context manager for reading/writing this realm's qbo_tokens row.

    Resolves the owning org from the realm (via the RLS-EXCLUDED organization mapping) and
    pins org+realm, falling back to the vetted bypass for an orphaned token (no org mapping).
    Shared by get_qbo_client_from_db, force_refresh_qbo_access_token, and
    mark_qbo_reconnect_required so tenant-scoping is IDENTICAL on every token read/write
    (INCIDENT-qbo-tokens-rls.md — an org-keyed read with no GUC fails closed to zero rows).
    """
    org = organization_for_company(db, str(realm_id))
    if org is not None:
        return tenant_scope_live(db, org_id=org.id, realm_id=str(realm_id))
    # CROSS-TENANT-OK: orphaned token (no org mapping); single-row realm-exact fetch, the
    # unique realm_id filter + app-layer authorization is the gate.
    return tenant_scope_live(db, bypass=True)


def force_refresh_qbo_access_token(db: Session, realm_id: str) -> str:
    # PLAN-EXEMPT: QBO connection-health revocation-detection — the 401 force-refresh seam (AC-1).
    """Force a single-flight refresh of this realm's access token; return the new plaintext.

    The ``on_auth_refresh`` callback wired into QBOClient. A 401 means the stored token is
    server-invalid regardless of its clock expiry, so this refreshes with ``force=True`` (the
    double-check keys on ROTATION, not the clock). If the refresh reports the grant dead
    (invalid_grant / 4xx / refresh-token-expired) ``_refresh_qbo_token`` flags the row + raises
    the 409 reconnect signal; if the row is already flagged this short-circuits to the same
    signal. Never bypasses the single-flight lock.
    """
    with _qbo_token_scope(db, realm_id):
        token = db.query(QBOToken).filter(QBOToken.realm_id == str(realm_id)).first()
        if not token:
            raise qbo_not_connected_exception()
        if token.reconnect_required:
            raise qbo_reconnect_required_exception()
        expected_access = token.access_token
        token = _refresh_qbo_token(db, token, force=True, expected_access=expected_access)
        _assert_and_persist_qbo_token_scope(db, token)
        return decrypt_secret(token.access_token)


def mark_qbo_reconnect_required(db: Session, realm_id: str, reason: str) -> None:
    # PLAN-EXEMPT: QBO connection-health revocation-detection — the on_auth_dead seam (AC-2).
    """Flag this realm's connection dead + raise the 409 reconnect signal (always raises).

    The ``on_auth_dead`` callback wired into QBOClient: a forced refresh rotated a token that
    is STILL rejected (``retry_still_401``) — the grant is dead. Flags the row (clears the
    stale expiry, records the event) and raises the reconnect signal.
    """
    with _qbo_token_scope(db, realm_id):
        token = db.query(QBOToken).filter(QBOToken.realm_id == str(realm_id)).first()
        if token is not None:
            _mark_token_reconnect_required(db, token, reason)
    raise qbo_reconnect_required_exception()


# PLAN-EXEMPT: chunk C6 (AC-7.1) of ratified plan 20260706-agent-path-remediation —
# app/qbo_client.py is C6's declared write surface; active-plan surface_scope omits it.
def get_qbo_client_from_db(
    db: Session,
    realm_id: str,
    lane: "str | None" = None,
) -> QBOClient:
    """
    Return a QBOClient for the given realm_id.

    SECURITY: realm_id is REQUIRED. The old legacy fallback to `query.first()`
    has been severed to prevent cross-tenant token sharing.

    Also:
      - Checks access_expires_at
      - If expired or expiring soon, calls Intuit token endpoint to refresh
      - Persists new tokens in Postgres

    ``lane`` (C6, optional): pins the returned client's per-realm-semaphore lane
    ("interactive" | "background"). Default ``None`` keeps ambient resolution
    (``qbo_lane`` context override → Celery-task detection → interactive), so
    every existing caller is unchanged: request-path callers keep the full cap
    and Celery-task callers are auto-tagged background.
    """
    import logging as _logging
    _logger = _logging.getLogger(__name__)

    if not realm_id:
        raise HTTPException(
            status_code=400,
            detail="CRITICAL: realm_id is required to fetch a QBO client. Cross-tenant fallback locked."
        )

    realm_id_str = str(realm_id)

    # RLS: ``qbo_tokens`` is ORG-keyed under live RLS (app.current_org_id), but
    # background/Celery callers pin only ``realm`` scope (or none), so the org GUC
    # is unset and the token row reads back as ZERO rows (fail-closed) — bricking
    # every QBO refresh/read path (INCIDENT-qbo-tokens-rls.md). Resolve the owning
    # org from the realm via the RLS-EXCLUDED organization/organization_companies
    # tables (readable under any scope) and pin the org GUC for the whole
    # read/refresh/persist block. In-request callers already pinned org+realm via
    # _check_company_access, so this re-pins the SAME org (harmless). Fall back to
    # the vetted system bypass ONLY when the realm has no org mapping (an orphaned
    # token whose organization_id was SET NULL on org deletion).
    # PLAN-EXEMPT: QBO connection-health — shared _qbo_token_scope (org-resolution + RLS pin).
    with _qbo_token_scope(db, realm_id_str):
        token = db.query(QBOToken).filter(QBOToken.realm_id == realm_id_str).first()

        if not token:
            # PRECONDITION, not an auth failure: 409 qbo_not_connected (never 401).
            raise qbo_not_connected_exception()

        # Verify the token realm matches what was requested (defence-in-depth)
        if realm_id and token.realm_id != str(realm_id):
            raise HTTPException(
                status_code=403,
                detail=f"QBO token realm mismatch: requested {realm_id}, found {token.realm_id}"
            )
        _assert_and_persist_qbo_token_scope(db, token)

        # AC-4 (revocation-detection): a connection already known-dead surfaces the reconnect
        # signal BEFORE any refresh attempt — the UI shows "Reconnect", never a masked 400/500.
        if token.reconnect_required:
            raise qbo_reconnect_required_exception()

        now = datetime.utcnow()
        # Consider token "stale" if it expires within the next 5 minutes
        if (
            token.access_expires_at
            and token.access_expires_at <= now + timedelta(minutes=5)
        ):
            token = _refresh_qbo_token(db, token)
            _assert_and_persist_qbo_token_scope(db, token)

        # AC-1 seam: the 401 self-heal callbacks close over db + realm_id_str (the seam that
        # HAS them) and route the forced refresh through the single-flight lock. Every caller
        # that goes through get_qbo_client_from_db self-heals a 401 with zero call-site changes.
        def _on_auth_refresh() -> str:
            return force_refresh_qbo_access_token(db, realm_id_str)

        def _on_auth_dead(reason: str) -> None:
            mark_qbo_reconnect_required(db, realm_id_str, reason)

        return QBOClient(  # PLAN-EXEMPT: C6 lane threading + QBO connection-health 401 self-heal seam
            access_token=decrypt_secret(token.access_token),
            realm_id=token.realm_id,
            lane=lane,
            on_auth_refresh=_on_auth_refresh,
            on_auth_dead=_on_auth_dead,
        )


@contextlib.contextmanager
def qbo_review_read_budget(budget, operation_key):
    """New read lane only: one physical request/deadline budget across refresh."""
    budget.remaining()
    marker = _qbo_review_budget.set((budget, operation_key))
    try:
        yield
    finally:
        _qbo_review_budget.reset(marker)


def _review_realm_semaphore(realm_id, lane, budget):
    """Atomic strict source semaphore; no legacy proceed-without-limit fallback."""
    from .accounting_sources.contracts import SourceContractError, BudgetExceeded
    if not settings.redis_url:
        raise SourceContractError("source semaphore is unavailable")
    socket_timeout = budget.timeout(2)
    client = redis.Redis.from_url(settings.redis_url, socket_timeout=socket_timeout,
                                  socket_connect_timeout=socket_timeout)
    key, member = f"qbo:sem:{realm_id}", uuid.uuid4().hex
    cap = _lane_cap(_resolve_qbo_lane(lane))
    expires = math.ceil(budget.remaining()) + 5
    acquire_deadline = time.monotonic() + min(QBO_SEMAPHORE_ACQUIRE_TIMEOUT, budget.remaining())
    acquired = False
    script = """redis.call('ZREMRANGEBYSCORE',KEYS[1],0,ARGV[1]);
if redis.call('ZCARD',KEYS[1]) < tonumber(ARGV[2]) then
redis.call('ZADD',KEYS[1],ARGV[3],ARGV[4]); redis.call('EXPIRE',KEYS[1],ARGV[5]); return 1;
end; return 0;"""
    try:
        while not acquired:
            budget.remaining()
            if time.monotonic() >= acquire_deadline:
                raise BudgetExceeded("source semaphore acquire budget exhausted")
            try:
                budget.admit_wait(socket_timeout * 2)
                now = time.time()
                acquired = bool(client.eval(script, 1, key, now, cap, now + expires, member, expires * 2))
            except redis.RedisError:
                raise SourceContractError("source semaphore is unavailable") from None
            budget.remaining()
            if not acquired:
                budget.wait(QBO_SEMAPHORE_POLL_SECONDS)
        yield
    finally:
        if acquired:
            # Bounded cleanup releases the slot; no financial send follows expiry.
            with contextlib.suppress(redis.RedisError):
                client.zrem(key, member)
        client.close()


_review_realm_semaphore = contextlib.contextmanager(_review_realm_semaphore)


def _review_response_bytes(response, budget, *, cap=None):
    """Bound response memory and recheck the absolute clock between chunks."""
    from .accounting_sources.contracts import BudgetExceeded
    maximum = budget.limits.response_bytes if cap is None else min(cap, budget.limits.response_bytes)
    chunks, size = [], 0
    try:
        for chunk in response.iter_content(chunk_size=65536):
            budget.remaining()
            size += len(chunk)
            if size > maximum:
                raise BudgetExceeded("source response exceeds its byte ceiling")
            chunks.append(chunk)
        budget.remaining()
        return b"".join(chunks)
    finally:
        response.close()


def _review_refresh_post(headers, data, budget, operation_key):
    """Credential POST only, charged to the same logical source read operation."""
    from .accounting_sources.contracts import SourceContractError
    endpoint = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
    if settings.intuit_token_endpoint != endpoint:
        raise SourceContractError("review refresh endpoint is not approved")
    budget.admit_request(operation_key)
    response = _review_http_exchange("POST", endpoint, headers=headers, data=data,
                                     budget=budget, cap=524288)
    raw = _review_response_bytes(response, budget, cap=524288)
    budget.account_response(raw)
    if 200 <= response.status_code < 400 and response.status_code != 200:
        raise SourceContractError("review refresh requires the documented final HTTP 200 response")
    from .accounting_sources.transport import strict_json_loads
    strict_json_loads(raw, maximum_bytes=524288, maximum_depth=16)
    budget.remaining()
    # Preserve the established refresh/rotation parser and atomic commit mechanics.
    bounded = requests.Response()
    bounded.status_code = response.status_code
    bounded.headers = response.headers
    bounded._content = raw
    bounded._content_consumed = True
    budget.remaining()
    return bounded


def _review_http_exchange(method, endpoint, *, headers, params=None, data=None, budget, cap=None):
    """One review-only HTTP exchange with product-owned process custody.

    The response adapter contains already bounded bytes. The streaming observer
    remains useful for memory/clock checks; it never owns a live socket here.
    """
    from .accounting_sources.contracts import BudgetExceeded, SourceContractError
    from .accounting_sources.qbo_http import (CustodyFailure, DeadlineExceeded,
                                              TransportRefused, exchange)
    budget.remaining()
    maximum = budget.limits.response_bytes if cap is None else min(cap, budget.limits.response_bytes)
    try:
        snapshot = exchange(method, endpoint, headers=headers, params=params, data=data,
                            budget=budget, cap=maximum)
    except DeadlineExceeded:
        raise BudgetExceeded("review HTTP absolute deadline exhausted") from None
    except CustodyFailure:
        raise SourceContractError("review HTTP terminal custody is unproved") from None
    except TransportRefused:
        # A cancelled refresh may have reached Intuit. Do not retry it here or
        # translate an uncertain network outcome into a definitive dead grant.
        raise SourceContractError("review HTTP exchange was refused; remote outcome may be unknown") from None
    budget.remaining()
    bounded = requests.Response()
    bounded.status_code = snapshot.status_code
    bounded.headers = requests.structures.CaseInsensitiveDict(snapshot.headers)
    bounded._content = snapshot.content
    bounded._content_consumed = True
    budget.remaining()
    return bounded


def build_review_qbo_sender(db, context, budget):
    """Raw read factory from a current server-resolved company/book context.

    Credential refresh uses a separate non-bypass session, so its single-flight
    commit cannot commit source staging or receipt publication. This factory
    never calls the legacy proactive builder or its nested retry methods.
    """
    from .accounting_sources.contracts import SourceContractError
    from .accounting_sources.transport import ProviderRead, RawResponse, validate_read
    from .models import AccountingBook, AccountingSourceConnection, OrganizationCompany
    from .services.review_company_scope import ReviewCompanyContext
    if not (settings.accounting_source_feeds_enabled and settings.accounting_source_live_reads_enabled):
        raise SourceContractError("live source reads are disabled")
    if type(context) is not ReviewCompanyContext:
        raise SourceContractError("server-resolved review context required")
    if (context.connection.provider != "qbo" or context.connection.status != "active"
            or context.book.status != "active" or not context.qbo_realm_id
            or context.company.realm_id != context.qbo_realm_id
            or context.connection.external_entity_id != context.qbo_realm_id
            or context.connection.external_account_id != context.qbo_realm_id
            or not re.fullmatch(r"[0-9]{1,64}", context.qbo_realm_id)):
        raise SourceContractError("QBO source identity is unavailable")
    if context.connection.environment not in {"sandbox", "production"}:
        raise SourceContractError("QBO environment is unavailable")
    realm_id = context.qbo_realm_id
    # Admission releases source locks before network waits. Capture only scalar
    # identity here; an expired ORM context must never trigger implicit PK reads.
    expected_identity = (context.organization_id, context.company_id, context.connection_id,
                         context.book_id, context.connection.external_account_id,
                         context.connection.external_entity_id, context.connection.environment,
                         context.book.external_book_id, context.book.accounting_basis, context.book.currency)
    host = ("sandbox-quickbooks.api.intuit.com" if expected_identity[6] == "sandbox"
            else "quickbooks.api.intuit.com")

    def send(read, *, timeout):
        validate_read(read)
        if not isinstance(read, ProviderRead) or read.provider != "qbo" or read.method != "GET":
            raise SourceContractError("fixed QBO raw GET required")
        scope = read.scope
        if (scope.organization_id, scope.company_id, scope.connection_id, scope.book_id,
                scope.external_account_id, scope.external_entity_id, scope.environment,
                scope.external_book_id, scope.basis, scope.currency) != expected_identity:
            raise SourceContractError("QBO raw read scope mismatch")
        budget.remaining()
        if budget.attempts.get(read.operation_key, 0) <= 0:
            raise SourceContractError("raw send lacks transport budget admission")
        # A new session owns only this exact legacy credential binding. Never use
        # _qbo_token_scope's orphan bypass or a source/task-selected realm.
        credential_bind = db.get_bind()
        credential_engine = credential_bind if isinstance(credential_bind, Engine) else credential_bind.engine
        with Session(bind=credential_engine) as credentials:
            with tenant_scope_live(credentials, org_id=context.organization_id,
                                   realm_id=realm_id, company_id=None, bypass=False):
                company = credentials.query(OrganizationCompany).filter(
                    OrganizationCompany.id == context.company_id,
                    OrganizationCompany.organization_id == context.organization_id,
                    OrganizationCompany.realm_id == realm_id).populate_existing().one_or_none()
                if company is None:
                    raise SourceContractError("canonical QBO company binding changed")
                token = credentials.query(QBOToken).filter(
                    QBOToken.realm_id == realm_id, QBOToken.organization_id == context.organization_id,
                    QBOToken.organization_company_id == context.company_id).populate_existing().one_or_none()
                if token is None:
                    raise qbo_not_connected_exception()
                if token.reconnect_required:
                    raise qbo_reconnect_required_exception()
                with qbo_review_read_budget(budget, read.operation_key):
                    if not token.access_expires_at or token.access_expires_at <= datetime.utcnow() + timedelta(seconds=30):
                        token = _refresh_qbo_token(credentials, token)
                    def physical_get(*, extra=False):
                        if extra:
                            budget.admit_request(read.operation_key)
                        budget.remaining()
                        with qbo_realm_semaphore(realm_id, lane=QBO_LANE_BACKGROUND):
                            response = _review_http_exchange("GET", "https://" + host + read.path,
                                headers={"Authorization": "Bearer " + decrypt_secret(token.access_token),
                                         "Accept": "application/json"}, params=dict(read.query),
                                budget=budget)
                            raw = _review_response_bytes(response, budget)
                        return RawResponse(response.status_code, dict(response.headers), raw)
                    response = physical_get()
                    if response.status == 401:
                        # The initial response also consumes bytes; transport only
                        # sees the final response returned by this sender.
                        budget.account_response(response.body)
                        expected_access = token.access_token
                        token = _refresh_qbo_token(credentials, token, force=True, expected_access=expected_access)
                        response = physical_get(extra=True)
                        if response.status == 401:
                            budget.remaining()
                            _mark_token_reconnect_required(credentials, token, QBO_HEALTH_RETRY_STILL_401)
                            budget.remaining()
                            raise qbo_reconnect_required_exception()
                    budget.remaining()
                    return response
    return send

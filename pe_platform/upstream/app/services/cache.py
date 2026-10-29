"""Company-scoped response cache.

SECURITY / SERIALIZATION POLICY (Phase 10 hardening):
- This module is JSON-only. It MUST NOT import or call ``pickle`` (or any other
  arbitrary-object deserializer) on values read from Redis. Deserializing
  untrusted bytes with pickle is a remote-code-execution risk, so it is
  permanently forbidden here.
- Cached values are wrapped in a small versioned envelope and stored as JSON
  text. On read we parse the JSON and validate the envelope; ANY decode error,
  version mismatch, or missing envelope is treated as a cache MISS (never an
  exception). This also guarantees that legacy pickle bytes left in Redis can
  never be unpickled -- they simply fail JSON parsing and are treated as misses.
- All Redis keys written/read/cleared by this module carry a version prefix
  (``v1:``) so that any pre-existing (pickle-era) keys are structurally
  unreachable by the new code path.
- Only JSON-serializable values are cached. Non-serializable values are skipped
  with a logged warning rather than crashing the caller.
"""

from typing import Any, Dict, Optional
import time
import logging
import json

import redis

from ..config import is_dev_environment, settings
from ..services.redaction import redact_sensitive

logger = logging.getLogger(__name__)

# JSON-only serialization. Pickle is intentionally NOT imported. Do not add it.
# The cache envelope version. Bump this if the envelope shape changes.
ENVELOPE_VERSION = 1

# Key namespace version prefix. Bump this to invalidate every key structurally
# (e.g. when migrating away from a prior, unsafe serialization format).
KEY_VERSION_PREFIX = "v1:"

# In-memory fallback is intentionally local-development/test only. Production
# deployments use Redis so cache invalidation is shared across web instances.
_CACHE_STORE: Dict[str, tuple[float, Any]] = {}
_REDIS_CLIENT: Optional[redis.Redis] = None
_REDIS_DISABLED = False


def _is_dev_environment() -> bool:
    return is_dev_environment(settings.environment)


def _prefixed(key: str) -> str:
    """Return the version-prefixed form of a logical cache key."""
    if key.startswith(KEY_VERSION_PREFIX):
        return key
    return f"{KEY_VERSION_PREFIX}{key}"


def _encode_value(value: Any) -> Optional[str]:
    """Serialize a value into a versioned JSON envelope.

    Returns the JSON text, or ``None`` if the value is not JSON-serializable
    (caller should skip caching in that case).
    """
    try:
        return json.dumps({"v": ENVELOPE_VERSION, "data": value})
    except (TypeError, ValueError) as exc:
        logger.warning(
            "Cache skip: value not JSON-serializable error=%s type=%s",
            exc,
            type(value).__name__,
        )
        return None


def _decode_value(raw: Any) -> tuple[bool, Any]:
    """Parse a stored JSON envelope.

    Returns ``(found, value)``. ``found`` is False on any decode error, version
    mismatch, or missing/invalid envelope -- i.e. it is treated as a cache miss.
    This never raises and never deserializes anything other than JSON, so legacy
    pickle bytes simply fail to parse and become a miss.
    """
    if raw is None:
        return False, None
    try:
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8")
        envelope = json.loads(raw)
    except (TypeError, ValueError, UnicodeDecodeError):
        return False, None

    if not isinstance(envelope, dict):
        return False, None
    if envelope.get("v") != ENVELOPE_VERSION:
        return False, None
    if "data" not in envelope:
        return False, None
    return True, envelope["data"]


def _redis_client() -> Optional[redis.Redis]:
    global _REDIS_CLIENT, _REDIS_DISABLED
    if _REDIS_DISABLED or not settings.redis_url:
        return None
    if _REDIS_CLIENT is None:
        try:
            _REDIS_CLIENT = redis.Redis.from_url(settings.redis_url, socket_timeout=2, socket_connect_timeout=2)
            _REDIS_CLIENT.ping()
        except Exception as exc:
            _REDIS_DISABLED = True
            if _is_dev_environment():
                logger.warning("Redis cache unavailable; using in-memory dev/test fallback: %s", exc)
            else:
                logger.error("Redis cache unavailable; production cache reads/writes disabled: %s", exc)
            return None
    return _REDIS_CLIENT


def get_cached(key: str) -> Optional[Any]:
    prefixed = _prefixed(key)
    client = _redis_client()
    if client is not None:
        try:
            raw = client.get(prefixed)
        except Exception as exc:
            logger.warning("Redis cache get failed key=%s error=%s", prefixed, exc)
            return None
        found, value = _decode_value(raw)
        return value if found else None
    if not _is_dev_environment():
        return None

    entry = _CACHE_STORE.get(prefixed)
    if not entry:
        return None

    expiry, value = entry
    if time.time() > expiry:
        # Cache expired
        del _CACHE_STORE[prefixed]
        return None

    return value


def set_cached(key: str, value: Any, ttl_seconds: int = 300) -> None:
    prefixed = _prefixed(key)
    safe_value = redact_sensitive(value)

    # Only JSON-serializable values are cacheable. If serialization fails we log
    # and skip rather than crashing the caller. We compute this up front so the
    # in-memory fallback enforces the same constraint as Redis.
    encoded = _encode_value(safe_value)
    if encoded is None:
        return

    client = _redis_client()
    if client is not None:
        try:
            client.setex(prefixed, ttl_seconds, encoded)
        except Exception as exc:
            logger.warning("Redis cache set failed key=%s error=%s", prefixed, exc)
        return
    if not _is_dev_environment():
        return

    # In-memory fallback: store the live object (we are not deserializing
    # untrusted bytes here, so the envelope is unnecessary). We still required
    # the value to be JSON-serializable above for consistency with Redis.
    expiry = time.time() + ttl_seconds
    _CACHE_STORE[prefixed] = (expiry, safe_value)


def delete_cached(key: str) -> None:
    """Best-effort delete of a single cache key (Redis or the dev in-memory store).

    Never raises: a failed delete only means the entry lives out its TTL.
    """
    prefixed = _prefixed(key)
    client = _redis_client()
    if client is not None:
        try:
            client.delete(prefixed)
        except Exception as exc:
            logger.warning("Redis cache delete failed key=%s error=%s", prefixed, exc)
        return
    _CACHE_STORE.pop(prefixed, None)


def clear_company_cache(company_id: str) -> None:
    """Wipes all cache keys associated with a given company.
    Useful when a Sync or Webhook signals fresh data.
    """
    # Match against version-prefixed keys so we clear exactly what this module
    # writes. (Legacy unprefixed/pickle keys are intentionally not matched --
    # they are already unreachable and will expire on their own TTL.)
    prefix = f"{KEY_VERSION_PREFIX}{company_id}:"
    scoped_fragment = f":realm:{company_id}:"
    client = _redis_client()
    if client is not None:
        patterns = [f"{prefix}*", f"{KEY_VERSION_PREFIX}*{scoped_fragment}*"]
        deleted = 0
        for pattern in patterns:
            for raw_key in client.scan_iter(match=pattern, count=500):
                deleted += client.delete(raw_key)
        if deleted:
            logger.info("[Cache] Cleared %s stale Redis cache entries for company_id=%s", deleted, company_id)
        return
    if not _is_dev_environment():
        return

    keys_to_delete = [
        k for k in _CACHE_STORE.keys()
        if k.startswith(prefix) or scoped_fragment in k
    ]
    for k in keys_to_delete:
        del _CACHE_STORE[k]

    if keys_to_delete:
        logger.info("[Cache] Cleared %s stale in-memory cache entries for company_id=%s", len(keys_to_delete), company_id)

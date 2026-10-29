import base64
import hashlib
import logging
from typing import Any, Optional

import requests

from ..config import settings
from .cache import delete_cached, get_cached, set_cached

logger = logging.getLogger(__name__)

# Short-TTL cache of an already-Stytch-validated session, keyed on a SHA-256 of
# the session token (never the raw token). A cache HIT lets the auth edge skip
# BOTH the blocking ``POST /v1/b2b/sessions/authenticate`` network call AND the
# per-request session-mirror ``db.commit()`` — on 100% of authenticated traffic
# in production, where the dev-JWT fast path is rejected. The cache is a
# performance backstop only: it is FAIL-CLOSED (a miss / decode error / expired
# entry always falls through to the real ``authenticate_session``, which 401s on
# failure exactly as before) and it NEVER authenticates on a miss. Identity
# guarantees are unchanged — see ``app/auth.py``'s Stytch arm.
#
# The TTL is bounded by the Stytch session length AND a hard ceiling so a cached
# entry never outlives the session and the revocation / role-change / MFA-state
# staleness window stays small (the same small window a local ``session_jwt``
# validation would accept). Reuse of the canonical Redis cache seam
# (``app/services/cache.py``) means ``REDIS_URL`` unset in dev falls back to the
# in-memory store, and any Redis error is already swallowed there → a miss.
_SESSION_CACHE_KEY_PREFIX = "auth:session:"
_SESSION_CACHE_TTL_MINUTES_CAP = 5
_SESSION_CACHE_ENVELOPE_VERSION = 1


def _session_cache_ttl_seconds() -> int:
    minutes = min(int(settings.auth_session_minutes), _SESSION_CACHE_TTL_MINUTES_CAP)
    return max(0, minutes) * 60


def _session_cache_key(token: str) -> str:
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return f"{_SESSION_CACHE_KEY_PREFIX}{digest}"


def get_cached_session_identity(token: str) -> Optional[dict[str, Any]]:  # CRAFT-OK: dynamic Stytch session-identity envelope boundary
    """Return the cached identity envelope for a session token, or ``None``.

    ``None`` is returned for a blank token, a disabled/zero TTL, a cache miss, a
    decode error, or an envelope-version mismatch — every one of which is a
    fail-closed signal to perform the real ``authenticate_session``. NEVER treat
    ``None`` as "authenticated"; it always means "go validate with Stytch."
    """
    if not token or _session_cache_ttl_seconds() <= 0:
        return None
    cached = get_cached(_session_cache_key(token))
    if not isinstance(cached, dict):
        return None
    if cached.get("v") != _SESSION_CACHE_ENVELOPE_VERSION:
        return None
    user_id = cached.get("user_id")
    if not isinstance(user_id, int):
        return None
    return cached


def set_cached_session_identity(
    token: str,
    *,
    user_id: int,
    organization_id: Optional[int],
    membership_id: Optional[int],
    stytch_session: dict[str, Any],  # CRAFT-OK: dynamic Stytch authenticate_session response payload
) -> None:
    """Cache the validated identity PKs + the Stytch session payload.

    Only primitives + the (already-JSON) Stytch payload are stored — never an
    ORM object (session-bound, un-serializable). On a hit the auth edge
    re-materializes fresh ORM rows by PK from the request's own ``db`` session,
    so the returned ``AuthContext`` is byte-identical to a freshly-validated one.
    The raw token is never stored: the key is its SHA-256.
    """
    ttl = _session_cache_ttl_seconds()
    if not token or ttl <= 0:
        return
    envelope = {
        "v": _SESSION_CACHE_ENVELOPE_VERSION,
        "user_id": user_id,
        "organization_id": organization_id,
        "membership_id": membership_id,
        "stytch_session": stytch_session,
    }
    set_cached(_session_cache_key(token), envelope, ttl)


def evict_cached_session_identity(token: str) -> None:
    """Drop the cached identity envelope for a session token (logout/explicit revocation).

    Without this, a token the user just revoked keeps cache-hitting — and therefore
    keeps authenticating — for up to the ≤5-minute TTL. Best-effort: a failed delete
    only means the entry lives out that TTL, exactly the pre-eviction behavior.
    """
    if not token:
        return
    delete_cached(_session_cache_key(token))


class StytchB2BError(RuntimeError):
    def __init__(self, status_code: int, detail: Any):
        self.status_code = status_code
        self.detail = detail
        super().__init__(str(detail))


class StytchB2BClient:
    def __init__(self) -> None:
        self.base_url = settings.stytch_api_base
        creds = f"{settings.stytch_project_id}:{settings.stytch_secret}".encode("utf-8")
        self._auth_header = f"Basic {base64.b64encode(creds).decode('ascii')}"

    @property
    def enabled(self) -> bool:
        return bool(settings.stytch_project_id and settings.stytch_secret)

    def _headers(self, session_token: Optional[str] = None, session_jwt: Optional[str] = None) -> dict[str, str]:
        headers = {
            "Authorization": self._auth_header,
            "Content-Type": "application/json",
        }
        if session_token:
            headers["X-Stytch-Member-Session"] = session_token
        if session_jwt:
            headers["X-Stytch-Member-SessionJWT"] = session_jwt
        return headers

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[dict[str, Any]] = None,
        session_token: Optional[str] = None,
        session_jwt: Optional[str] = None,
    ) -> dict[str, Any]:
        if not self.enabled:
            raise StytchB2BError(503, "Stytch is not configured")

        response = requests.request(
            method=method,
            url=f"{self.base_url}{path}",
            json=json_body,
            headers=self._headers(session_token=session_token, session_jwt=session_jwt),
            timeout=20,
        )
        if response.ok:
            return response.json() if response.content else {}
        try:
            detail = response.json()
        except Exception:
            detail = response.text
        logger.warning("Stytch API %s %s failed: %s", method, path, detail)
        raise StytchB2BError(response.status_code, detail)

    def create_organization(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/v1/b2b/organizations", json_body=payload)

    def update_organization(self, organization_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("PUT", f"/v1/b2b/organizations/{organization_id}", json_body=payload)

    def delete_organization(self, organization_id: str) -> dict[str, Any]:
        return self._request("DELETE", f"/v1/b2b/organizations/{organization_id}")

    def create_member(self, organization_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/v1/b2b/organizations/{organization_id}/members",
            json_body=payload,
        )

    def invite_member(self, payload: dict[str, Any], session_token: Optional[str] = None) -> dict[str, Any]:
        return self._request(
            "POST",
            "/v1/b2b/magic_links/email/invite",
            json_body=payload,
            session_token=session_token,
        )

    def update_member(self, organization_id: str, member_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request(
            "PUT",
            f"/v1/b2b/organizations/{organization_id}/members/{member_id}",
            json_body=payload,
        )

    def delete_member(self, organization_id: str, member_id: str) -> dict[str, Any]:
        return self._request(
            "DELETE",
            f"/v1/b2b/organizations/{organization_id}/members/{member_id}",
        )

    def reactivate_member(self, organization_id: str, member_id: str) -> dict[str, Any]:
        return self._request(
            "PUT",
            f"/v1/b2b/organizations/{organization_id}/members/{member_id}/reactivate",
        )

    def search_members(self, organization_id: str) -> dict[str, Any]:
        return self._request(
            "POST",
            "/v1/b2b/organizations/members/search",
            json_body={"organization_ids": [organization_id], "query": {"operator": "OR"}},
        )

    def migrate_password(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/v1/b2b/passwords/migrate", json_body=payload)

    def authenticate_password(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/v1/b2b/passwords/authenticate", json_body=payload)

    def authenticate_magic_link(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/v1/b2b/magic_links/authenticate", json_body=payload)

    def password_reset_by_email_start(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/v1/b2b/passwords/email/reset/start", json_body=payload)

    def password_reset_by_email_complete(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/v1/b2b/passwords/email/reset", json_body=payload)

    def update_member_email(self, organization_id: str, member_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self.update_member(organization_id, member_id, payload)

    def authenticate_session(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/v1/b2b/sessions/authenticate", json_body=payload)

    def revoke_session(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/v1/b2b/sessions/revoke", json_body=payload)

    def exchange_session(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/v1/b2b/sessions/exchange", json_body=payload)

    def create_totp(self, payload: dict[str, Any], session_token: Optional[str] = None) -> dict[str, Any]:
        return self._request("POST", "/v1/b2b/totp", json_body=payload, session_token=session_token)

    def authenticate_totp(self, payload: dict[str, Any], session_token: Optional[str] = None) -> dict[str, Any]:
        return self._request(
            "POST",
            "/v1/b2b/totp/authenticate",
            json_body=payload,
            session_token=session_token,
        )

    def send_sms_otp(self, payload: dict[str, Any], session_token: Optional[str] = None) -> dict[str, Any]:
        return self._request(
            "POST",
            "/v1/b2b/otps/sms/send",
            json_body=payload,
            session_token=session_token,
        )

    def authenticate_sms_otp(self, payload: dict[str, Any], session_token: Optional[str] = None) -> dict[str, Any]:
        return self._request(
            "POST",
            "/v1/b2b/otps/sms/authenticate",
            json_body=payload,
            session_token=session_token,
        )


stytch_b2b_client = StytchB2BClient()

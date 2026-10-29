from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from ..models import QBOCompanyInfo, QBOToken


def _iso_or_none(value: Any) -> Optional[str]:
    return value.isoformat() if value else None


def _utc_naive(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def qbo_connection_metadata(
    *,
    token: Optional[QBOToken],
    info: Optional[QBOCompanyInfo],
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Return non-secret QBO connection diagnostics for company switchers/debug UI."""
    now = _utc_naive(now) or datetime.now(timezone.utc).replace(tzinfo=None)
    has_token = token is not None
    refresh_expires_at = _utc_naive(getattr(token, "refresh_expires_at", None))
    access_expires_at = _utc_naive(getattr(token, "access_expires_at", None))

    disconnect_incomplete = getattr(token, "revoke_status", None) == "revoke_failed"
    # PLAN-EXEMPT: QBO connection-health revocation-detection — the server-confirmed dead-grant
    # signal. A grant revoked out-of-band on Intuit's side is clock-fresh but server-invalid, so
    # it is NOT captured by the expiry checks below — reconnect_required is the honest signal,
    # ranking ABOVE the expiry statuses (a dead grant is a reconnect, not a refresh).
    reconnect_required = bool(getattr(token, "reconnect_required", False))

    if not token:
        status = "disconnected"
    elif disconnect_incomplete:
        # The tenant asked to disconnect but the Intuit-side revoke failed;
        # the row is kept only so the revoke can be retried. Reporting this
        # as "connected" would hide the incomplete disconnect from admins.
        status = "disconnect_incomplete"
    elif reconnect_required:
        # The OAuth grant was revoked/invalidated on Intuit's side → only a user
        # reconnect via /qbo/authorize restores it (no refresh can heal it).
        status = "reconnect_required"
    elif refresh_expires_at and refresh_expires_at <= now:
        status = "reauth_required"
    elif access_expires_at and access_expires_at <= now:
        status = "refresh_required"
    else:
        status = "connected"

    connected = has_token and not disconnect_incomplete and not reconnect_required
    return {
        "connected": connected,
        "qbo_connected": connected,
        "qbo_connection_status": status,
        "qbo_disconnect_incomplete": disconnect_incomplete,
        "qbo_reconnect_required": reconnect_required,
        "qbo_last_health_error": getattr(token, "last_health_error", None),
        "qbo_health_checked_at": _iso_or_none(getattr(token, "health_checked_at", None)),
        "qbo_revoke_attempted_at": _iso_or_none(getattr(token, "revoke_attempted_at", None)),
        "qbo_access_expires_at": _iso_or_none(getattr(token, "access_expires_at", None)),
        "qbo_refresh_expires_at": _iso_or_none(getattr(token, "refresh_expires_at", None)),
        "qbo_company_info_synced": info is not None,
        "qbo_company_info_updated_at": _iso_or_none(getattr(info, "updated_at", None)),
    }

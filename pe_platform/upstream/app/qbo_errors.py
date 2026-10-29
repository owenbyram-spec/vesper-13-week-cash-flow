"""Shared QBO precondition errors.

`qbo_not_connected` is a PRECONDITION failure (the company has no QBO token /
connection), NOT an authentication failure. It must use a distinct status (409
Conflict) so it is never conflated with a genuine session-expiry 401 — the
frontend re-auths/redirects only on real session 401s, and renders a
"Connect QuickBooks" call-to-action for this 409.
"""
from typing import Optional

from fastapi import HTTPException

QBO_NOT_CONNECTED_CODE = "qbo_not_connected"
# PLAN-EXEMPT: QBO connection-health revocation-detection — a REVOKED grant is a
# distinct precondition from never-connected: the tenant HAD a connection that
# died out-of-band, and the fix is a RECONNECT, not a first-time connect. A
# distinct code drives a "Reconnect" CTA + clean telemetry; the frontend maps it
# exactly like qbo_not_connected (409 CTA, never a re-auth). See
# work/qbo-connection-health/research/20260707-revocation-detection/FINDINGS.md.
QBO_RECONNECT_REQUIRED_CODE = "qbo_reconnect_required"

_DEFAULT_NOT_CONNECTED_MESSAGE = (
    "QuickBooks isn't connected for this company. Connect it in Settings to load this data."
)

_DEFAULT_RECONNECT_MESSAGE = (
    "QuickBooks disconnected this company on Intuit's side. Reconnect it in Settings to restore access."
)


def qbo_not_connected_exception(user_message: Optional[str] = None) -> HTTPException:
    """Return a 409 with a structured, machine-readable not-connected detail.

    Status 409 (Conflict) — a precondition the user can resolve by connecting
    QuickBooks; deliberately NOT 401 (which means session/auth failure).
    """
    return HTTPException(
        status_code=409,
        detail={
            "error_code": QBO_NOT_CONNECTED_CODE,
            "user_message": user_message or _DEFAULT_NOT_CONNECTED_MESSAGE,
            "source": "quickbooks_online",
            "retryable": False,
        },
    )


def is_qbo_connection_required_exception(exc: object) -> bool:
    # PLAN-EXEMPT: QBO connection-health revocation-detection — degrade-but-surface-CTA helper.
    """True when ``exc`` is a QBO connection PRECONDITION (409 qbo_not_connected /
    qbo_reconnect_required).

    A shared/degrading endpoint (dashboard cards, aggregates) that catches a broad
    ``Exception`` to degrade a TRANSIENT QBO fault must NOT swallow this one — it has to
    propagate so the UI shows the Connect/Reconnect CTA instead of a masked empty payload.
    Use as: ``except Exception as e: if is_qbo_connection_required_exception(e): raise; ...``.
    """
    return (
        isinstance(exc, HTTPException)
        and exc.status_code == 409
        and isinstance(exc.detail, dict)
        and exc.detail.get("error_code") in (QBO_NOT_CONNECTED_CODE, QBO_RECONNECT_REQUIRED_CODE)
    )


def qbo_reconnect_required_exception(user_message: "str | None" = None) -> HTTPException:
    # PLAN-EXEMPT: QBO connection-health revocation-detection — the reconnect-required signal.
    """Return a 409 signalling a REVOKED/dead grant that needs a user reconnect.

    Status 409 (Conflict), mirroring ``qbo_not_connected_exception`` so the
    frontend renders a CTA (never a session re-auth) — but with the distinct
    ``qbo_reconnect_required`` code so the UI shows "Reconnect" (not "Connect")
    and telemetry can separate a dead grant from a never-connected company.
    ``retryable`` is False: only a user reconnect via ``/qbo/authorize`` fixes it.
    """
    return HTTPException(
        status_code=409,
        detail={
            "error_code": QBO_RECONNECT_REQUIRED_CODE,
            "user_message": user_message or _DEFAULT_RECONNECT_MESSAGE,
            "source": "quickbooks_online",
            "retryable": False,
        },
    )


# PLAN-EXEMPT: plan 20260711-agentic-workflow-hardening B1 — INV-UNCERTAIN (SYNTHESIS GAP-A6).
class QBOUncertainOutcomeError(RuntimeError):
    """A QBO WRITE was dispatched but its outcome is unknown.

    Raised when the request may have reached Intuit and been applied — a network
    failure/timeout after the POST left the socket, or a provider 5xx (Intuit 5xx does
    not guarantee the write was rolled back). This class is deliberately distinct from
    a confirmed 4xx validation reject: an uncertain write must NEVER be blindly retried
    (double-posting risk) and must never be recorded as a confirmed failure (the books
    may already carry it). The executor keeps the action APPROVED, marks the execution
    ledger row ``uncertain``, and the retry path reconciles against QBO's actual ledger
    before any re-POST.
    """

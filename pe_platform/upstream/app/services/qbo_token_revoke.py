"""
app/services/qbo_token_revoke.py
─────────────────────────────────
Revokes a QuickBooks Online OAuth grant at Intuit. Revoking the REFRESH token
kills the whole grant (access token included), so disconnect callers pass the
stored (encrypted) refresh token.

Log/response hygiene (hard requirement):
- NEVER log or return the Intuit response body, the refresh token (plaintext or
  encrypted), or any other token material.
- On failure, only an HTTP status code and a short error CATEGORY string are
  logged/returned (e.g. "http_401", "network_error", "decrypt_failed").
"""
from __future__ import annotations

import base64
import logging
from collections.abc import Callable, Iterable

import requests
from sqlalchemy import event
from sqlalchemy.orm import Session

from ..config import settings
from .crypto import decrypt_secret

logger = logging.getLogger(__name__)

REVOKE_TIMEOUT_SECONDS = 30

# Intuit answers 400 when the token is already revoked/invalid. For disconnect
# idempotency we treat that as success: the grant is gone either way.
CATEGORY_ALREADY_REVOKED = "already_revoked"


def revoke_intuit_token(refresh_token_encrypted: str | None) -> tuple[bool, str | None]:
    """
    Revoke the OAuth grant for the given (encrypted) refresh token at Intuit.

    Returns ``(success, category)``:
    - ``(True, None)`` — Intuit confirmed the revoke (HTTP 200/204).
    - ``(True, "already_revoked")`` — Intuit returned 400 (token already
      revoked/invalid); treated as success for idempotency.
    - ``(False, <category>)`` — revoke failed; category is a short string such
      as ``http_401``, ``network_error``, ``decrypt_failed``, ``missing_token``.

    Never returns or logs token material or response bodies.
    """
    if not refresh_token_encrypted:
        logger.warning("Intuit token revoke failed: category=missing_token")
        return False, "missing_token"

    try:
        refresh_token = decrypt_secret(refresh_token_encrypted)
    except Exception:
        logger.warning("Intuit token revoke failed: category=decrypt_failed")
        return False, "decrypt_failed"

    if not refresh_token:
        logger.warning("Intuit token revoke failed: category=missing_token")
        return False, "missing_token"

    basic = base64.b64encode(
        f"{settings.qbo_client_id}:{settings.qbo_client_secret}".encode("utf-8")
    ).decode("ascii")

    try:
        resp = requests.post(
            settings.intuit_revoke_endpoint,
            headers={
                "Authorization": f"Basic {basic}",
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={"token": refresh_token},
            timeout=REVOKE_TIMEOUT_SECONDS,
        )
    except requests.RequestException:
        # Do not log the exception itself: connection errors can embed the
        # request (including the token form body) in their repr.
        logger.warning("Intuit token revoke failed: category=network_error")
        return False, "network_error"

    if resp.status_code in (200, 204):
        logger.info("Intuit token revoke succeeded: status=%s", resp.status_code)
        return True, None

    if resp.status_code == 400:
        logger.info(
            "Intuit token revoke: status=400 category=%s (treated as success)",
            CATEGORY_ALREADY_REVOKED,
        )
        return True, CATEGORY_ALREADY_REVOKED

    category = f"http_{resp.status_code}"
    logger.warning(
        "Intuit token revoke failed: status=%s category=%s",
        resp.status_code,
        category,
    )
    return False, category


def revoke_purged_grants_on_commit(
    db: Session,
    grants: Iterable[tuple[str, str | None]],
    *,
    log_prefix: str,
    revoke: Callable[[str | None], tuple[bool, str | None]] = revoke_intuit_token,
) -> None:
    """Revoke ``grants`` at Intuit when — and only if — ``db``'s current
    (outermost) transaction commits.

    ``grants`` are ``(realm_id, encrypted refresh token)`` pairs of QBO token
    rows the caller has deleted in that transaction. The revoke is a network
    call no rollback can undo, so it must never run for a purge that rolls
    back: that would leave a tenant whose QuickBooks grants are revoked while
    its data and tokens remain. A savepoint commit or rollback does not decide.
    """
    pending = list(grants)
    if not pending:
        return

    def _after_commit(session: Session) -> None:
        if session.in_nested_transaction() or not pending:
            return
        committed = pending[:]
        pending.clear()
        revoke_purged_grants(committed, log_prefix=log_prefix, revoke=revoke)

    def _after_transaction_end(session: Session, transaction) -> None:
        # Fires after _after_commit on a commit. Any other end of the outermost
        # transaction (rollback, or close() mid-transaction) discards the purge,
        # so a later commit on the same session must not revoke for it.
        if transaction.parent is None:
            pending.clear()

    event.listen(db, "after_commit", _after_commit)
    event.listen(db, "after_transaction_end", _after_transaction_end)


def revoke_purged_grants(
    grants: Iterable[tuple[str, str | None]],
    *,
    log_prefix: str,
    revoke: Callable[[str | None], tuple[bool, str | None]] = revoke_intuit_token,
) -> None:
    """Revoke the grants of QBO token rows a committed purge already deleted.

    Best effort: every failure is logged with a short category (never token
    material or response bodies) and swallowed — the local rows are gone.
    """
    for realm_id, refresh_token in grants:
        try:
            revoked, category = revoke(refresh_token)
        except Exception:
            logger.warning("%s: Intuit revoke raised for realm_id=%s (token row already purged)", log_prefix, realm_id)
            continue
        if not revoked:
            logger.warning(
                "%s: Intuit revoke failed for realm_id=%s category=%s (token row already purged)",
                log_prefix,
                realm_id,
                category,
            )

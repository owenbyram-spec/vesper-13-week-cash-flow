"""Per-tenant LLM credential store — encrypt-on-write, decrypt-at-use.

The ONE service seam over `TenantLLMCredential`. It reuses the app's single AEAD
crypto seam (`app.services.crypto.encrypt_secret` / `decrypt_secret`) — there is
no second crypto path. The plaintext key is:
  - encrypted via `encrypt_secret` BEFORE it is persisted (`set_tenant_llm_keys`);
  - decrypted via `decrypt_secret` ONLY at point-of-use (`get_tenant_llm_keys`);
  - NEVER returned in an API response (the router returns masked metadata only);
  - NEVER written to a log (this module logs no key material — only org ids and
    booleans).

Tenant scoping: one row per `organization_id` (UNIQUE). Callers pass the
session-derived `organization_id`; the live Postgres RLS backstop keys on the same
column. When a row is absent OR `enabled` is False, every provider resolves to
`None` so the caller falls back to the global key.
"""
from __future__ import annotations

import logging
from typing import TypedDict

from sqlalchemy.orm import Session

from .crypto import decrypt_secret, encrypt_secret
from ..models import TenantLLMCredential

logger = logging.getLogger(__name__)


class TenantLLMKeys(TypedDict):
    anthropic: str | None
    openai: str | None


def get_credential_row(
    db: Session, organization_id: int
) -> TenantLLMCredential | None:
    """Return the tenant's credential row (or None). Scoped by organization_id."""
    return (
        db.query(TenantLLMCredential)
        .filter(TenantLLMCredential.organization_id == organization_id)
        .first()
    )


def get_tenant_llm_keys(db: Session, organization_id: int) -> TenantLLMKeys:
    """Resolve a tenant's decrypted LLM keys.

    Returns ``{"anthropic": <key|None>, "openai": <key|None>}``. A provider is
    ``None`` when its column is unset OR when the row is absent OR when the row's
    ``enabled`` flag is False — in every such case the caller falls back to the
    global ``settings.*_api_key``. The decrypted plaintext is returned to the
    in-process resolver ONLY; it is never logged here and must never be placed in
    an API response.
    """
    row = get_credential_row(db, organization_id)
    if row is None or not row.enabled:
        return TenantLLMKeys(anthropic=None, openai=None)
    return TenantLLMKeys(
        anthropic=decrypt_secret(row.anthropic_api_key_encrypted),
        openai=decrypt_secret(row.openai_api_key_encrypted),
    )


def set_tenant_llm_keys(
    db: Session,
    organization_id: int,
    *,
    anthropic_api_key: str | None = None,
    openai_api_key: str | None = None,
    provider_anthropic_workspace_id: str | None = None,
    provider_openai_project_id: str | None = None,
    enabled: bool | None = None,
    model_overrides: dict | None = None,
    update_anthropic: bool = False,
    update_openai: bool = False,
) -> TenantLLMCredential:
    """Create or update a tenant's LLM credential row, encrypting before persist.

    Each plaintext key is run through ``encrypt_secret`` (AEAD, enc:v1: envelope)
    BEFORE assignment — the column never holds plaintext. ``encrypt_secret`` is
    idempotent on an already-prefixed value, so re-passing an envelope is safe.

    The ``update_anthropic`` / ``update_openai`` flags distinguish "set this
    provider's key" from "leave this provider untouched": a None key with the flag
    set CLEARS the column (a rotation-to-empty / disable-one-provider), while a
    None key with the flag unset leaves the existing ciphertext in place. The
    caller does NOT commit; the router owns the transaction boundary.

    No key material is logged — only the org id and which providers changed.
    """
    row = get_credential_row(db, organization_id)
    if row is None:
        row = TenantLLMCredential(organization_id=organization_id)
        db.add(row)

    if update_anthropic:
        row.anthropic_api_key_encrypted = encrypt_secret(anthropic_api_key)
    if update_openai:
        row.openai_api_key_encrypted = encrypt_secret(openai_api_key)

    if provider_anthropic_workspace_id is not None:
        row.provider_anthropic_workspace_id = provider_anthropic_workspace_id
    if provider_openai_project_id is not None:
        row.provider_openai_project_id = provider_openai_project_id
    if enabled is not None:
        row.enabled = enabled
    if model_overrides is not None:
        row.model_overrides = model_overrides

    db.flush()
    logger.info(
        "tenant LLM credentials updated org_id=%s anthropic_changed=%s "
        "openai_changed=%s enabled=%s",
        organization_id,
        update_anthropic,
        update_openai,
        row.enabled,
    )
    return row


def fingerprint_last4(encrypted_value: str | None) -> str | None:
    """Return the last 4 chars of the DECRYPTED key for a masked fingerprint.

    Decrypts ONLY to derive the trailing 4 characters; the full plaintext never
    leaves this function. Returns None when the column is unset. Used by the admin
    GET to show ``…ab12`` without ever exposing the key.
    """
    if not encrypted_value:
        return None
    plaintext = decrypt_secret(encrypted_value)
    if not plaintext:
        return None
    return plaintext[-4:]

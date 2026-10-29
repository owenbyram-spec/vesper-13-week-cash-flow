from __future__ import annotations

import hashlib
import json
import secrets
import urllib.parse
from datetime import datetime, timedelta
from typing import Any, Mapping, Optional

from sqlalchemy.orm import Session

from ...models import AgentConfirmationToken


# ─────────────────────────────────────────────────────────────────────────────
# Canonical action fingerprint (Phase 6)
#
# A confirmation token must not be reusable for a *materially different* action.
# The fingerprint binds a token to the exact action it was issued for, derived
# ONLY from trusted, backend-controlled scope plus the normalized tool input —
# never from model-provided scope fields. On consume, the caller supplies the
# fingerprint of the action it is about to authorize; we require an exact match.
# ─────────────────────────────────────────────────────────────────────────────

# Keys that describe *scope*, not *action payload*. They are stripped from the
# tool input before fingerprinting so that (a) a model cannot influence the
# fingerprint by smuggling scope into the payload, and (b) trusted scope is the
# single authority for who/what the action targets. Trusted scope is folded into
# the fingerprint separately from the (now scope-free) payload.
_SCOPE_AND_VOLATILE_KEYS = frozenset(
    {
        "organization_id",
        "organization_company_id",
        "company_id",
        "qbo_realm_id",
        "realm_id",
        "user_id",
        "tenant_id",
        "confirmation_token",
        "confirmation_tokens",
        "request_id",
        "timestamp",
        "ts",
        "_ts",
        "nonce",
        "created_at",
    }
)


def _canonicalize(value: Any) -> Any:
    """
    Recursively normalize a JSON-ish value so that semantically identical
    payloads produce identical fingerprints regardless of key order or trivial
    numeric representation differences.

    - dict keys are sorted (also enforced by json sort_keys, but we recurse so
      nested dicts are normalized for numeric coercion too)
    - integral floats (100.0) collapse to ints (100) so "100" amount changes are
      still caught while 100 == 100.0 matches
    - bools are preserved (and must not be confused with ints)
    - strings are left as-is (we deliberately do NOT coerce "100" -> 100, since
      that could mask a genuinely different action)
    """
    if isinstance(value, Mapping):
        return {str(k): _canonicalize(value[k]) for k in sorted(value, key=str)}
    if isinstance(value, (list, tuple)):
        return [_canonicalize(v) for v in value]
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else value
    return value


def _strip_scope(tool_input: Optional[Mapping[str, Any]]) -> dict:
    """Return the tool input with scope/volatile keys removed."""
    if not tool_input:
        return {}
    return {
        str(k): v
        for k, v in tool_input.items()
        if str(k) not in _SCOPE_AND_VOLATILE_KEYS
    }


def compute_action_fingerprint(
    *,
    tool_name: str,
    user_id: int,
    company_id: str,
    organization_id: Optional[int] = None,
    organization_company_id: Optional[int] = None,
    qbo_realm_id: Optional[str] = None,
    tool_input: Optional[Mapping[str, Any]] = None,
    action_type: Optional[str] = None,
    risk_level: Optional[str] = None,
    proposed_action_id: Optional[int] = None,
) -> str:
    """
    Produce a stable SHA-256 fingerprint for an action.

    The fingerprint is computed exclusively from trusted scope (passed by the
    backend, never by the model) plus the scope-stripped, normalized tool input.
    Two calls with the same semantic action — even with different JSON key order
    or 100 vs 100.0 — produce the same fingerprint; any change to amount, period,
    account, target company, tool, or user produces a different one.
    """
    canonical = {
        "tool_name": tool_name,
        "user_id": user_id,
        "company_id": company_id,
        "organization_id": organization_id,
        "organization_company_id": organization_company_id,
        "qbo_realm_id": qbo_realm_id,
        "action_type": action_type,
        "risk_level": risk_level,
        "proposed_action_id": proposed_action_id,
        "tool_input": _canonicalize(_strip_scope(tool_input)),
    }
    serialized = json.dumps(canonical, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _payload_hash(tool_input: Optional[Mapping[str, Any]]) -> Optional[str]:
    if tool_input is None:
        return None
    serialized = json.dumps(
        _canonicalize(_strip_scope(tool_input)),
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _hash_confirmation_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def issue_agent_confirmation_token(
    db: Session,
    *,
    user_id: int,
    company_id: str,
    tool_name: str,
    tenant_id: Optional[str] = None,
    organization_id: Optional[int] = None,
    organization_company_id: Optional[int] = None,
    qbo_realm_id: Optional[str] = None,
    tool_input: Optional[Mapping[str, Any]] = None,
    action_type: Optional[str] = None,
    risk_level: Optional[str] = None,
    proposed_action_id: Optional[int] = None,
    action_fingerprint: Optional[str] = None,
    ttl_seconds: int = 300,
) -> tuple[str, AgentConfirmationToken]:
    """
    Issue a one-time confirmation token.

    Fingerprint binding (Phase 6): if ``tool_input`` is provided, the canonical
    action fingerprint is computed server-side from trusted scope and stored on
    the token; any ``action_fingerprint`` argument is ignored in that case so a
    caller can never inject an attacker-chosen fingerprint. If neither is
    provided, the token carries no fingerprint and consume falls back to
    scope-only checks (legacy behavior, preserved for back-compat).
    """
    if tool_input is not None:
        action_fingerprint = compute_action_fingerprint(
            tool_name=tool_name,
            user_id=user_id,
            company_id=company_id,
            organization_id=organization_id,
            organization_company_id=organization_company_id,
            qbo_realm_id=qbo_realm_id,
            tool_input=tool_input,
            action_type=action_type,
            risk_level=risk_level,
            proposed_action_id=proposed_action_id,
        )

    # NOTE: qbo_realm_id / action_type / risk_level / proposed_action_id and the
    # raw payload are all folded into action_fingerprint above; they are NOT
    # stored as separate columns. The AgentConfirmationToken table intentionally
    # persists only the hash (Phase 6: "store hash, not raw sensitive payload"),
    # so the fingerprint is the single binding artifact.
    secret = secrets.token_urlsafe(32)
    token_row = AgentConfirmationToken(
        token_hash=_hash_confirmation_secret(secret),
        user_id=user_id,
        company_id=company_id,
        tenant_id=tenant_id,
        organization_id=organization_id,
        organization_company_id=organization_company_id,
        tool_name=tool_name,
        action_fingerprint=action_fingerprint,
        expires_at=datetime.utcnow() + timedelta(seconds=ttl_seconds),
    )
    db.add(token_row)
    db.flush()
    raw = json.dumps({"id": token_row.id, "secret": secret}, separators=(",", ":"))
    return urllib.parse.quote(raw, safe=""), token_row


def consume_agent_confirmation_token(
    db: Session,
    token: str,
    *,
    user_id: int,
    company_id: str,
    tenant_id: Optional[str] = None,
    organization_id: Optional[int] = None,
    organization_company_id: Optional[int] = None,
    expected_fingerprint: Optional[str] = None,
    enforce_fingerprint: bool = True,
) -> AgentConfirmationToken:
    """
    Validate and consume a one-time confirmation token.

    Enforcement order: structural validity -> single-use -> expiry -> secret
    match -> scope match -> fingerprint match. A token that was issued with an
    action fingerprint REQUIRES a matching ``expected_fingerprint`` (computed by
    the caller from the action it is about to authorize); a missing or mismatched
    fingerprint is rejected. Tokens issued without a fingerprint keep the legacy
    scope-only behavior.

    Model-provided text such as "the user confirmed" can never satisfy this — a
    valid backend-issued, unexpired, unused, scope- and fingerprint-matching
    token is the only thing that passes.
    """
    try:
        raw = urllib.parse.unquote(token)
        data = json.loads(raw)
        token_id = data["id"]
        secret = data["secret"]
    except Exception as exc:
        raise ValueError("Invalid confirmation token") from exc

    token_row = db.query(AgentConfirmationToken).filter(AgentConfirmationToken.id == token_id).first()
    if not token_row:
        raise ValueError("Invalid confirmation token")
    if token_row.used_at:
        raise ValueError("Confirmation token has already been used")
    if token_row.expires_at.replace(tzinfo=None) < datetime.utcnow():
        raise ValueError("Confirmation token has expired")
    if not secrets.compare_digest(token_row.token_hash, _hash_confirmation_secret(secret)):
        raise ValueError("Invalid confirmation token")
    if token_row.user_id != user_id:
        raise ValueError("Confirmation token belongs to another user")
    if token_row.company_id != company_id:
        raise ValueError("Confirmation token belongs to another company")
    if token_row.tenant_id and tenant_id and token_row.tenant_id != tenant_id:
        raise ValueError("Confirmation token belongs to another tenant")
    if token_row.organization_id is not None and organization_id is not None and token_row.organization_id != organization_id:
        raise ValueError("Confirmation token belongs to another organization")
    if (
        token_row.organization_company_id is not None
        and organization_company_id is not None
        and token_row.organization_company_id != organization_company_id
    ):
        raise ValueError("Confirmation token belongs to another company entity")

    # Fingerprint binding: a token issued for a specific action can only
    # authorize that exact action. Skipped when ``enforce_fingerprint`` is False
    # (two-phase flow — the binding is enforced later at tool-execution time).
    if enforce_fingerprint and token_row.action_fingerprint is not None:
        if not expected_fingerprint or not secrets.compare_digest(
            token_row.action_fingerprint, expected_fingerprint
        ):
            raise ValueError("Confirmation token does not match this action")

    token_row.used_at = datetime.utcnow()
    token_row.used_by_user_id = user_id
    db.commit()
    db.refresh(token_row)
    return token_row

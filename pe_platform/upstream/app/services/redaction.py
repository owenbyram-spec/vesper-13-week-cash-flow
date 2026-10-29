from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Optional

SENSITIVE_KEY_RE = re.compile(
    r"(access[_-]?token|refresh[_-]?token|id[_-]?token|session[_-]?token|session[_-]?jwt|"
    r"authorization|client[_-]?secret|"
    r"api[_-]?key|password|secret|oauth|bearer)",
    re.IGNORECASE,
)
BEARER_OR_BASIC_RE = re.compile(r"\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE)
JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")
KEY_VALUE_SECRET_RE = re.compile(
    r"(?i)\b(access[_-]?token|refresh[_-]?token|id[_-]?token|session[_-]?token|session[_-]?jwt|"
    r"authorization|client[_-]?secret|"
    r"api[_-]?key|password|secret)=([^&\s,}]+)"
)
JSON_SECRET_RE = re.compile(
    r'(?i)("?(?:access[_-]?token|refresh[_-]?token|id[_-]?token|session[_-]?token|session[_-]?jwt|'
    r'authorization|'
    r'client[_-]?secret|api[_-]?key|password|secret)"?\s*:\s*")([^"]+)(")'
)


def redact_sensitive(value: Any, *, max_length: int | None = None) -> Any:
    """Return a log-safe copy of value with token/secret material removed."""
    if isinstance(value, dict):
        return {
            str(key): "[redacted]" if SENSITIVE_KEY_RE.search(str(key)) else redact_sensitive(item, max_length=max_length)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_sensitive(item, max_length=max_length) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_sensitive(item, max_length=max_length) for item in value)
    if not isinstance(value, str):
        return value

    text = BEARER_OR_BASIC_RE.sub(lambda match: f"{match.group(1)} [redacted]", value)
    text = JWT_RE.sub("[redacted-jwt]", text)
    text = KEY_VALUE_SECRET_RE.sub(lambda match: f"{match.group(1)}=[redacted]", text)
    text = JSON_SECRET_RE.sub(lambda match: f"{match.group(1)}[redacted]{match.group(3)}", text)
    if max_length is not None and len(text) > max_length:
        text = f"{text[:max_length]}...[truncated]"
    return text


def redact_to_string(value: Any, *, max_length: int = 1000) -> str:
    redacted = redact_sensitive(value, max_length=max_length)
    if isinstance(redacted, str):
        return redacted
    try:
        text = json.dumps(redacted, default=str, sort_keys=True)
    except TypeError:
        text = str(redacted)
    if len(text) > max_length:
        return f"{text[:max_length]}...[truncated]"
    return text


# ─── Audit payload summarization (H4) ─────────────────────────────────────────
# Audit-log `details` must not duplicate raw financial payloads. The system of
# record for a proposed write is ProposedAction.payload (and the QBO entity
# itself); the audit trail only needs to prove WHAT kind of change happened,
# WHO did it, and WHICH payload (by stable fingerprint) — never the values.


def payload_fingerprint(payload: Any) -> str:
    """Stable sha256 fingerprint of the canonical JSON form of a payload.

    The same logical payload always produces the same fingerprint (keys are
    sorted, separators fixed), so audit rows can be correlated with the source
    record without retaining any field values.
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def payload_field_names(payload: Any, *, _prefix: str = "") -> list[str]:
    """Sorted, value-free summary of which fields a payload contains.

    Nested dict keys become dotted paths; list items are collapsed to a single
    ``[]`` segment so the summary shape does not scale with (or leak) the number
    of distinct values. Only key NAMES are ever included — never values.
    """
    names: set[str] = set()
    if isinstance(payload, dict):
        for key, item in payload.items():
            path = f"{_prefix}.{key}" if _prefix else str(key)
            names.add(path)
            names.update(payload_field_names(item, _prefix=path))
    elif isinstance(payload, (list, tuple)):
        for item in payload:
            names.update(payload_field_names(item, _prefix=f"{_prefix}[]"))
    return sorted(names)


def audit_payload_summary(
    payload: Any,
    *,
    action: Optional[str] = None,
    entity_type: Optional[str] = None,
    entity_id: Optional[str] = None,
    actor_id: Optional[str] = None,
    organization_id: Optional[int] = None,
    company_id: Optional[str] = None,
    proposal_id: Optional[int] = None,
    execution_id: Optional[int] = None,
    idempotency_fingerprint: Optional[str] = None,
    before: Any = None,
    after: Any = None,
    status: Optional[str] = None,
    error_category: Optional[str] = None,
    correlation_id: Optional[str] = None,
    extra_refs: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Build an audit-safe summary of a write payload.

    Returns identifiers, fingerprints and a field-NAME summary only — no payload
    values are retained. ``extra_refs`` is for additional non-sensitive entity
    references (e.g. doc numbers, sync tokens); None values are dropped.
    """
    summary: dict[str, Any] = {
        "payload_fingerprint": payload_fingerprint(payload),
        "payload_fields": payload_field_names(payload),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if before is not None:
        summary["before_fingerprint"] = payload_fingerprint(before)
    if after is not None:
        summary["after_fingerprint"] = payload_fingerprint(after)
    scalars = {
        "action": action,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "actor_id": actor_id,
        "organization_id": organization_id,
        "company_id": company_id,
        "proposal_id": proposal_id,
        "execution_id": execution_id,
        "idempotency_fingerprint": idempotency_fingerprint,
        "status": status,
        "error_category": error_category,
        "correlation_id": correlation_id,
    }
    summary.update({key: value for key, value in scalars.items() if value is not None})
    for key, value in (extra_refs or {}).items():
        if value is not None:
            summary[key] = value
    return summary

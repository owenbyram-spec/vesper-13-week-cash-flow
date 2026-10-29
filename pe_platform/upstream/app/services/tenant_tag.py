"""Provider-agnostic tenant attribution tag (one Anthropic key per env).

All tenants share a single Anthropic key per environment, so per-tenant cost
attribution cannot rely on per-tenant credentials. Instead every billed LLM call
is stamped with an opaque, PII-free tenant tag:

  * Anthropic — ``metadata.user_id``
  * OpenAI    — top-level ``user``

The same hash is used for both providers so a given tenant tags identically
regardless of which lane/provider served the call.

The tag (and the realm triple it is derived from) is carried through the call
path on a :class:`contextvars.ContextVar`. The agent-run boundary binds it; lane
calls (``app/services/llm.py``) — which take no explicit tenant arguments — read
it to set the provider tag AND to backfill the realm triple on their
``LLMUsageEvent`` rows, which would otherwise be NULL and invisible to cost
rollups.

Invariant: the ContextVar defaults to ``None``. An unbound context yields an
untagged / NULL-realm row, which is correct. We NEVER inherit a stale or ambient
tenant — a NULL row merely omits attribution, whereas a wrong-tenant row would
silently corrupt another tenant's attribution (and, in Phase B, its budget).
"""

from __future__ import annotations

import contextlib
import hashlib
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterator, Optional

__all__ = [
    "TenantTagContext",
    "tenant_tag",
    "bind_tenant_tag",
    "current_tenant_context",
    "current_tenant_tag",
]


@dataclass(frozen=True)
class TenantTagContext:
    """Active tenant identity for the current execution context.

    Carries both the opaque provider tag and the realm triple so that
    lane-originated usage rows (which have no explicit tenant args) can be both
    tagged at the provider and stamped for our own attribution.
    """

    # PLAN-EXEMPT: pilot LLM-usage attribution fix (AC-4/AC-5); no on-disk plan doc for 20260625-pilot-bug-fixes
    tag: str
    organization_id: int | None = None
    organization_company_id: int | None = None
    qbo_realm_id: str | None = None
    user_id: int | None = None


# Default None: never inherit an ambient tenant (see module docstring).
_current: ContextVar[Optional[TenantTagContext]] = ContextVar(
    "current_tenant_tag", default=None
)


def tenant_tag(
    organization_id: Optional[int],
    organization_company_id: Optional[int],
    qbo_realm_id: Optional[str],
) -> str:
    """Return an opaque, deterministic, PII-free per-tenant tag.

    ``ten_`` + the first 32 hex chars of ``sha256("org:company:realm")``.
    Contains no email, realm name, or other PII — only internal numeric ids and
    the QBO realm id. Deterministic per tenant; differs across tenants.
    """
    raw = f"{organization_id}:{organization_company_id}:{qbo_realm_id}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
    return f"ten_{digest}"


@contextlib.contextmanager
def bind_tenant_tag(
    *,
    organization_id: int | None,  # PLAN-EXEMPT: pilot LLM-usage attribution (AC-5.1)
    organization_company_id: int | None = None,
    qbo_realm_id: str | None = None,
    user_id: int | None = None,
) -> Iterator[TenantTagContext | None]:
    """Bind the tenant identity for the current context (token-based reset).

    When no organization is known we bind ``None`` (shadowing any outer binding)
    rather than computing a tag over all-``None`` ids — that keeps genuinely
    non-tenant work untagged instead of collapsing every such call onto one
    shared hash, and guarantees a nested call never leaks an outer tenant.

    ``user_id`` is carried as a separate structured field for per-user usage
    attribution; it is NEVER folded into the opaque ``tenant_tag`` hash (which
    stays a pure per-tenant identifier). NULL on user-less system/background work.
    """
    if organization_id is None:
        ctx: TenantTagContext | None = None
    else:
        ctx = TenantTagContext(
            tag=tenant_tag(organization_id, organization_company_id, qbo_realm_id),
            organization_id=organization_id,
            organization_company_id=organization_company_id,
            qbo_realm_id=qbo_realm_id,
            user_id=user_id,
        )
    token = _current.set(ctx)
    try:
        yield ctx
    finally:
        _current.reset(token)


def current_tenant_context() -> Optional[TenantTagContext]:
    """Return the tenant identity bound to the current context, or None."""
    return _current.get()


def current_tenant_tag() -> Optional[str]:
    """Return the opaque tenant tag bound to the current context, or None."""
    ctx = _current.get()
    return ctx.tag if ctx is not None else None

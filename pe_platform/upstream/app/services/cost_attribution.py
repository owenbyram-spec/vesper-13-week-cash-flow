"""Per-tenant LLM cost attribution (read surface).

Rolls up the rows we already write to ``LLMUsageEvent`` (one per model call) into
a per-tenant / per-company spend summary. Reads only our own rows — it never
calls the Anthropic or OpenAI APIs. With one shared provider key per
environment, this table is the authoritative source of per-tenant cost.

For attribution to be complete, lane (analysis/context) calls must carry the
realm triple; that is backfilled from the tenant tag bound on the call context
(see ``app/services/tenant_tag.py``). The spend guard (``enforce_tenant_budget``)
sits on top of ``tenant_cost_summary``; ``lane_tenant_budget_verdict`` applies it
to every lane LLM call before provider spend.

A usage row with tokens but no ``estimated_cost_usd`` is priced at read time when
its model now has a pricing entry. If it still has none, month-to-date spend is a
lower bound only, so a budget cannot be shown to hold: the guard reports it
``unavailable`` rather than counting it as $0.
"""

from __future__ import annotations

import contextlib
import logging
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterator, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from ..agent.performance import estimate_model_cost_usd, is_model_priced
from ..models import EntityBilling, LLMUsageEvent, Organization, QBOCompanyInfo, TenantBilling

logger = logging.getLogger(__name__)

# Spend-guard thresholds (fraction of the monthly budget consumed month-to-date).
BUDGET_SOFT_THRESHOLD = Decimal("0.9")   # >= 90% → downgrade to cheaper routes
BUDGET_HARD_THRESHOLD = Decimal("1.0")   # >= 100% → block

# User-facing message returned when a workspace is over its hard budget. Plain,
# actionable, no internal numbers.
BUDGET_REACHED_MESSAGE = "Monthly AI budget reached for this workspace — contact your admin."
BUDGET_UNAVAILABLE_MESSAGE = (
    "This workspace's AI budget could not be verified, so no model call was made — try again or contact your admin."
)
UNPRICED_MODEL_MESSAGE = (
    "The configured AI model has no verified price, so its spend cannot be held to this workspace's budget."
)

# Refusal reasons a lane caller can branch on (``LLMCallResult.error_type``).
LANE_REFUSAL_BUDGET_BLOCKED = "budget_blocked"
LANE_REFUSAL_BUDGET_UNAVAILABLE = "budget_unavailable"
LANE_REFUSAL_UNPRICED_MODEL = "unpriced_model"


def _to_float(value: Any) -> float:
    """Coerce a SUM result (Decimal/None) to a plain float dollar amount."""
    if value is None:
        return 0.0
    if isinstance(value, Decimal):
        return float(value)
    return float(value)


def tenant_cost_summary(
    db: Session,
    *,
    organization_id: int,
    start: datetime,
    end: datetime,
    organization_company_id: Optional[int] = None,
) -> dict[str, Any]:
    """Summarize LLM spend for one tenant over ``[start, end)``.

    Sums ``estimated_cost_usd`` (USD), ``total_tokens``, and call count from
    ``LLMUsageEvent``, scoped to ``organization_id`` (and optionally one
    ``organization_company_id``) within the half-open window, grouped by company.

    Returns the org totals plus a per-company breakdown. Reads only this org's
    rows — a tenant never sees another tenant's spend.

    A row that used tokens but carries no cost was recorded while its model had
    no pricing entry. It is priced here when ``MODEL_PRICING_PER_MILLION`` now has
    one (exactly as ``estimate_model_cost_usd`` prices a new call); otherwise it is
    counted in ``unpriced_call_count``, ``cost_complete`` is False and
    ``total_cost_usd`` is a lower bound.
    """
    base_filters = [
        LLMUsageEvent.organization_id == organization_id,
        LLMUsageEvent.created_at >= start,
        LLMUsageEvent.created_at < end,
    ]
    if organization_company_id is not None:
        base_filters.append(LLMUsageEvent.organization_company_id == organization_company_id)

    rows = (
        db.query(
            LLMUsageEvent.organization_company_id.label("organization_company_id"),
            func.coalesce(func.sum(LLMUsageEvent.estimated_cost_usd), 0).label("cost_usd"),
            func.coalesce(func.sum(LLMUsageEvent.total_tokens), 0).label("total_tokens"),
            func.count(LLMUsageEvent.id).label("call_count"),
        )
        .filter(*base_filters)
        .group_by(LLMUsageEvent.organization_company_id)
        .all()
    )

    uncosted_rows = (
        db.query(
            LLMUsageEvent.organization_company_id.label("organization_company_id"),
            LLMUsageEvent.model.label("model"),
            func.coalesce(func.sum(LLMUsageEvent.input_tokens), 0).label("input_tokens"),
            func.coalesce(func.sum(LLMUsageEvent.output_tokens), 0).label("output_tokens"),
            func.count(LLMUsageEvent.id).label("call_count"),
        )
        .filter(
            *base_filters,
            LLMUsageEvent.estimated_cost_usd.is_(None),
            LLMUsageEvent.total_tokens > 0,
        )
        .group_by(LLMUsageEvent.organization_company_id, LLMUsageEvent.model)
        .all()
    )
    repriced_cost: dict[Optional[int], Decimal] = {}
    unpriced_calls: dict[Optional[int], int] = {}
    for row in uncosted_rows:
        cost = estimate_model_cost_usd(
            row.model,
            {"input_tokens": int(row.input_tokens or 0), "output_tokens": int(row.output_tokens or 0)},
        )
        company_id = row.organization_company_id
        if cost is None:
            unpriced_calls[company_id] = unpriced_calls.get(company_id, 0) + int(row.call_count or 0)
        else:
            repriced_cost[company_id] = repriced_cost.get(company_id, Decimal("0")) + cost

    by_company: list[dict[str, Any]] = []
    total_cost = 0.0
    total_tokens = 0
    total_calls = 0
    total_unpriced = 0
    for row in rows:
        cost = _to_float(row.cost_usd) + float(repriced_cost.get(row.organization_company_id, 0))
        tokens = int(row.total_tokens or 0)
        calls = int(row.call_count or 0)
        unpriced = unpriced_calls.get(row.organization_company_id, 0)
        by_company.append(
            {
                "organization_company_id": row.organization_company_id,
                "cost_usd": cost,
                "total_tokens": tokens,
                "call_count": calls,
                "unpriced_call_count": unpriced,
            }
        )
        total_cost += cost
        total_tokens += tokens
        total_calls += calls
        total_unpriced += unpriced

    return {
        "organization_id": organization_id,
        "organization_company_id": organization_company_id,
        "start": start,
        "end": end,
        "total_cost_usd": total_cost,
        "total_tokens": total_tokens,
        "call_count": total_calls,
        "unpriced_call_count": total_unpriced,
        "cost_complete": total_unpriced == 0,
        "by_company": by_company,
    }


def platform_token_usage_rollup(
    db: Session,
    *,
    window_days: int,
    organization_id: Optional[int] = None,
) -> dict[str, Any]:
    """PLAN-EXEMPT: ratified plan 20260702-qa-remediation W-1 (item 6).

    Platform-admin per-tenant token/cost rollup over the trailing
    ``window_days``, grouped from the ``llm_usage_events`` ledger. The response
    shape is the FROZEN W-1 contract the admin UI (agent B) builds against::

        {
          "tenants": [
            {
              "organization_id": int,
              "name": str,
              "total_tokens": int,
              "total_cost_usd": float,
              "by_task_type": {task_type: {"total_tokens": int, "total_cost_usd": float}},
              "realms": [
                {"realm_id": str, "company_name": str,
                 "total_tokens": int, "total_cost_usd": float}
              ],
            }
          ],
          "window_days": int,
        }

    ``organization_id`` narrows to one tenant (the per-realm drill). Rows with
    a NULL organization_id (genuinely non-tenant work, e.g. platform-level
    embeddings) have no tenant to roll up under and are excluded by design.
    Caller owns the platform-admin gate AND the RLS bypass pin (the router
    mirrors the other cross-tenant admin reads).
    """
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=window_days)
    base_filters = [
        LLMUsageEvent.created_at >= start,
        LLMUsageEvent.organization_id.isnot(None),
    ]
    if organization_id is not None:
        base_filters.append(LLMUsageEvent.organization_id == organization_id)

    token_sum = func.coalesce(func.sum(LLMUsageEvent.total_tokens), 0)
    cost_sum = func.coalesce(func.sum(LLMUsageEvent.estimated_cost_usd), 0)

    org_rows = (
        db.query(LLMUsageEvent.organization_id, token_sum, cost_sum)
        .filter(*base_filters)
        .group_by(LLMUsageEvent.organization_id)
        .all()
    )
    task_rows = (
        db.query(LLMUsageEvent.organization_id, LLMUsageEvent.task_type, token_sum, cost_sum)
        .filter(*base_filters)
        .group_by(LLMUsageEvent.organization_id, LLMUsageEvent.task_type)
        .all()
    )
    realm_rows = (
        db.query(LLMUsageEvent.organization_id, LLMUsageEvent.qbo_realm_id, token_sum, cost_sum)
        .filter(*base_filters, LLMUsageEvent.qbo_realm_id.isnot(None))
        .group_by(LLMUsageEvent.organization_id, LLMUsageEvent.qbo_realm_id)
        .all()
    )

    org_ids = [row[0] for row in org_rows]
    org_names = {
        org.id: org.name
        for org in db.query(Organization).filter(Organization.id.in_(org_ids or [0])).all()
    }
    realm_ids = sorted({row[1] for row in realm_rows if row[1]})
    company_names = {
        info.realm_id: info.company_name
        for info in db.query(QBOCompanyInfo)
        .filter(QBOCompanyInfo.realm_id.in_(realm_ids or [""]))
        .all()
    }

    by_task: dict[int, dict[str, dict[str, Any]]] = {}
    for org_id, task_type, tokens, cost in task_rows:
        by_task.setdefault(org_id, {})[task_type or "other"] = {
            "total_tokens": int(tokens or 0),
            "total_cost_usd": _to_float(cost),
        }
    realms_by_org: dict[int, list[dict[str, Any]]] = {}
    for org_id, realm_id, tokens, cost in realm_rows:
        realms_by_org.setdefault(org_id, []).append(
            {
                "realm_id": realm_id,
                "company_name": company_names.get(realm_id, realm_id),
                "total_tokens": int(tokens or 0),
                "total_cost_usd": _to_float(cost),
            }
        )

    tenants: list[dict[str, Any]] = []
    for org_id, tokens, cost in org_rows:
        realms = sorted(
            realms_by_org.get(org_id, []), key=lambda r: r["total_tokens"], reverse=True
        )
        tenants.append(
            {
                "organization_id": org_id,
                "name": org_names.get(org_id, str(org_id)),
                "total_tokens": int(tokens or 0),
                "total_cost_usd": _to_float(cost),
                "by_task_type": by_task.get(org_id, {}),
                "realms": realms,
            }
        )
    tenants.sort(key=lambda t: t["total_cost_usd"], reverse=True)

    return {"tenants": tenants, "window_days": window_days}


def current_month_window() -> tuple[datetime, datetime]:
    """Current calendar month-to-date ``[month_start, now)`` in UTC."""
    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return month_start, now


# ── Phase B: per-tenant spend guard ──────────────────────────────────────────
#
# Budget pressure is carried on a ContextVar (mirroring the tenant tag) so that
# non-tool / background lane work can bias toward cheaper routes during a run
# without threading a flag through every caller. Bound at the same synchronous
# span as the tenant tag (see model_loop._apply_tool_calls) to avoid the
# generator/streaming token-reset hazard. Default None = no pressure.
_budget_pressure: ContextVar[Optional[str]] = ContextVar("budget_pressure", default=None)


@contextlib.contextmanager
def bind_budget_pressure(action: Optional[str]) -> Iterator[Optional[str]]:
    """Bind the budget-pressure level for the current context (token reset).

    ``action`` is the enforce_tenant_budget verdict ("downgrade"); binding
    "allow"/None is a no-op pressure state.
    """
    level = action if action == "downgrade" else None
    token = _budget_pressure.set(level)
    try:
        yield level
    finally:
        _budget_pressure.reset(token)


def current_budget_pressure() -> Optional[str]:
    """Return the budget-pressure level bound to the current context, or None."""
    return _budget_pressure.get()


def enforce_tenant_budget(
    db: Session,
    *,
    organization_id: Optional[int],
    organization_company_id: Optional[int] = None,
) -> dict[str, str]:
    """Decide whether a tenant may spend more this calendar month.

    Reads the monthly budget (in CENTS) with company-level override precedence:
    an ``EntityBilling.monthly_budget_cents`` for the specific company wins and is
    measured against that company's spend; otherwise the org-wide
    ``TenantBilling.monthly_budget_cents`` is measured against org-wide spend —
    mirroring the existing token-limit logic in the runtime. NULL/unset/≤0 budget
    → unlimited (allow).

    Returns ``{"action": "allow"|"downgrade"|"block"|"unavailable", "reason": str}``. Soft
    threshold (≥90% of budget) → downgrade; hard (≥100%) → block. Month-to-date
    usage that has no price (``reason="unpriced_usage"``) is unavailable unless the
    priced spend alone already reaches the hard threshold.

    Lookup failures return unavailable: callers must stop model spending until
    budget evidence is available. This is not an atomic spend reservation.
    """
    return _tenant_budget_verdict(
        db, organization_id=organization_id, organization_company_id=organization_company_id
    )[0]


def _tenant_budget_verdict(
    db: Session,
    *,
    organization_id: Optional[int],
    organization_company_id: Optional[int] = None,
) -> tuple[dict[str, str], bool]:
    """``enforce_tenant_budget``'s verdict plus whether a budget applies at all."""
    try:
        if organization_id is None:
            return {"action": "allow", "reason": "no_org"}, False

        # Company-level override takes precedence (and is measured at company scope).
        budget_cents: Optional[int] = None
        company_scoped = False
        if organization_company_id is not None:
            entity = (
                db.query(EntityBilling)
                .filter(EntityBilling.organization_company_id == organization_company_id)
                .first()
            )
            if entity is not None and entity.monthly_budget_cents is not None:
                budget_cents = entity.monthly_budget_cents
                company_scoped = True
        if budget_cents is None:
            tenant = (
                db.query(TenantBilling)
                .filter(TenantBilling.organization_id == organization_id)
                .first()
            )
            if tenant is not None and tenant.monthly_budget_cents is not None:
                budget_cents = tenant.monthly_budget_cents

        if budget_cents is None or budget_cents <= 0:
            return {"action": "allow", "reason": "no_budget"}, False

        budget_usd = Decimal(int(budget_cents)) / Decimal(100)
        start, end = current_month_window()
        summary = tenant_cost_summary(
            db,
            organization_id=organization_id,
            start=start,
            end=end,
            organization_company_id=organization_company_id if company_scoped else None,
        )
        mtd_usd = Decimal(str(summary["total_cost_usd"]))
        ratio = mtd_usd / budget_usd

        if ratio >= BUDGET_HARD_THRESHOLD:
            return {
                "action": "block",
                "reason": f"month-to-date ${mtd_usd:.2f} >= budget ${budget_usd:.2f}",
            }, True
        if int(summary.get("unpriced_call_count") or 0) > 0:
            # Unpriced usage makes month-to-date a lower bound: the budget cannot be
            # shown to hold, so spending stops instead of counting it as $0.
            return {"action": "unavailable", "reason": "unpriced_usage"}, True
        if ratio >= BUDGET_SOFT_THRESHOLD:
            return {
                "action": "downgrade",
                "reason": f"month-to-date ${mtd_usd:.2f} >= 90% of budget ${budget_usd:.2f}",
            }, True
        return {
            "action": "allow",
            "reason": f"month-to-date ${mtd_usd:.2f} < 90% of budget ${budget_usd:.2f}",
        }, True
    except Exception:
        logger.warning("[cost_attribution] budget evidence unavailable")
        return {"action": "unavailable", "reason": "budget_lookup_unavailable"}, True


def lane_tenant_budget_verdict(model: str) -> dict[str, str]:
    """Pre-call tenant budget verdict for a lane LLM call that holds no db session.

    The tenant comes only from server-bound context: the tenant tag, else a
    non-bypass RLS scope, else the realm on the bound LLM task context (resolved to
    its org and company through the RLS-excluded organization tables). Prompt or
    model content never selects it. With no resolvable tenant the call is not
    tenant work and is allowed (``no_org``), exactly as ``enforce_tenant_budget``.

    Fails closed: any error reading budget state is ``unavailable``, and a model
    with no verified price is refused (``block``/``unpriced_model``) whenever a
    budget applies, because its spend could never be counted against it.
    """
    from ..tenant_rls import get_tenant_scope, tenant_scope_live
    from .llm_task_context import current_llm_task_context
    from .tenant_tag import current_tenant_context

    tag = current_tenant_context()
    organization_id = getattr(tag, "organization_id", None)
    organization_company_id = getattr(tag, "organization_company_id", None)
    realm_id = getattr(tag, "qbo_realm_id", None)
    if organization_id is None:
        scope = get_tenant_scope()
        if scope is not None and not scope.bypass:
            organization_id = scope.org_id
            realm_id = scope.realm_id
    if organization_id is None and not realm_id:
        task_ctx = current_llm_task_context()
        realm_id = getattr(task_ctx, "realm_id", None)
    if organization_id is None and not realm_id:
        return {"action": "allow", "reason": "no_org"}

    try:
        from ..db import SessionLocal
        from .tenant_companies import organization_company_for_realm, organization_for_company

        session = SessionLocal()
        try:
            if organization_id is None:
                organization = organization_for_company(session, str(realm_id))
                if organization is None:
                    return {"action": "allow", "reason": "no_org"}
                organization_id = organization.id
            if organization_company_id is None and realm_id:
                company = organization_company_for_realm(
                    session, organization_id=organization_id, realm_id=str(realm_id)
                )
                organization_company_id = company.id if company is not None else None
            # Own-org pin (not bypass): the billing and usage tables are org-keyed.
            with tenant_scope_live(session, org_id=organization_id):
                verdict, budget_applies = _tenant_budget_verdict(
                    session,
                    organization_id=organization_id,
                    organization_company_id=organization_company_id,
                )
        finally:
            session.close()
    except Exception:
        logger.warning("[cost_attribution] lane budget evidence unavailable for org=%s", organization_id)
        return {"action": "unavailable", "reason": "budget_lookup_unavailable"}

    if budget_applies and verdict["action"] in ("allow", "downgrade") and not is_model_priced(model):
        return {"action": "block", "reason": "unpriced_model"}
    return verdict


def lane_refusal_reason(verdict: dict[str, str]) -> Optional[str]:
    """Map a lane verdict to its refusal reason, or None when the call may proceed."""
    if verdict.get("action") == "block":
        if verdict.get("reason") == "unpriced_model":
            return LANE_REFUSAL_UNPRICED_MODEL
        return LANE_REFUSAL_BUDGET_BLOCKED
    if verdict.get("action") in ("allow", "downgrade"):
        return None
    return LANE_REFUSAL_BUDGET_UNAVAILABLE


LANE_REFUSAL_MESSAGES = {
    LANE_REFUSAL_BUDGET_BLOCKED: BUDGET_REACHED_MESSAGE,
    LANE_REFUSAL_BUDGET_UNAVAILABLE: BUDGET_UNAVAILABLE_MESSAGE,
    LANE_REFUSAL_UNPRICED_MODEL: UNPRICED_MODEL_MESSAGE,
}

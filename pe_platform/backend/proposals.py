"""Local review adapter over PE's real ProposedAction/execution ledger.

The vendor owns drafting and execution. This adapter adds exact local finance-base
binding and a no-egress, cloned-database impact review before human approval.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date, datetime, timezone
from decimal import Decimal
from functools import wraps
import hashlib
import hmac
import json
import secrets
import sqlite3
import threading

from fastapi import APIRouter, HTTPException, Request

ACTION_TO_TOOL = {
    "CASHFLOW_CELL_UPDATE": "update_cashflow_cells",
    "CASHFLOW_SCHEDULE_LAYER": "layer_cashflow_schedule",
    "CASHFLOW_ACTUALS_CATEGORIZATION": "categorize_cashflow_actuals",
    "CASHFLOW_SPREAD": "spread_cashflow_balance",
    "CASHFLOW_OVERRIDE_DELETE": "delete_cashflow_override",
    "MEMORY_UPDATE": "teach_vesper_bot",
}
_NO_PROVIDER = ContextVar("local_pe_preview_no_provider", default=False)
_FALLBACK_LOCK = threading.RLock()
_LISTENER_MODELS = set()
_EXECUTION_REVIEW = ContextVar("local_pe_approved_execution_review", default=None)


class ReviewedImpactMismatch(RuntimeError):
    pass


def install_execution_fence():
    """Retain native admission; fence only its approved handler's finance txn.

    Native cash handlers commit internally. During local approval their commits
    become flushes until the actual grid matches the exact reviewed grid. The
    durable native admission/owner ledger is outside this narrow fence.
    """
    from app.assistant.handlers import TOOL_HANDLERS

    for name in ACTION_TO_TOOL.values():
        original = TOOL_HANDLERS[name]
        if getattr(original, "_local_review_fence", False):
            continue

        @wraps(original)
        def guarded(db, company_id, tool_input, _handler=original, _name=name):
            review = _EXECUTION_REVIEW.get()
            if review is None:
                return _handler(db, company_id, tool_input)
            if review["db"] is not db or review["tool"] != _name or review["scope"]["realm_id"] != company_id:
                raise ReviewedImpactMismatch("The approved execution scope differs from its review")
            from .cashflow import cached_forecast
            from .connections import replay_source

            previous = db.__dict__.get("commit")
            commit = db.commit
            attempted_commit = False
            token = _NO_PROVIDER.set(True)
            try:
                db.commit = lambda: db.flush()
                with replay_source(review["scope"]):
                    result = _handler(db, company_id, tool_input)
                    if not isinstance(result, str) or result.startswith("Error:") or "error(s)" in result:
                        raise ReviewedImpactMismatch(
                            "The native handler could not complete the exact reviewed change"
                        )
                    db.flush()
                    actual = cached_forecast(review["runtime"], review["scope"], db)
                    if impact_digest(actual) != review["after_hash"]:
                        raise ReviewedImpactMismatch(
                            "The actual financial impact differed from the reviewed preview"
                        )
                    attempted_commit = True
                    commit()
                    review["financial_committed"] = True
                    return result
            except Exception:
                db.rollback()
                # A raised COMMIT may already have landed. Never assert rollback
                # certainty after a physical commit attempt; native recovery owns it.
                review["financial_rollback_confirmed"] = not attempted_commit
                raise
            finally:
                _NO_PROVIDER.reset(token)
                if previous is None:
                    del db.commit
                else:
                    db.commit = previous

        guarded._local_review_fence = True
        TOOL_HANDLERS[name] = guarded


def no_provider_calls() -> bool:
    return _NO_PROVIDER.get()


def canonical(value):
    def encode(member):
        if isinstance(member, (datetime, date)):
            return member.isoformat()
        if isinstance(member, Decimal):
            return str(member)
        raise TypeError("Unsupported review data")

    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False, default=encode
    )


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def impact_digest(value):
    """Compare native impact while excluding newly generated audit clocks.

    Categorization stamps reviewed_at on each execution. The actual wall clock
    belongs to the immutable approval export. The financial grid, period dates,
    coverage, statuses and transaction counts must still match exactly.
    """
    clocks = {
        "created_at",
        "updated_at",
        "reviewed_at",
        "reconciled_at",
        "pulled_at",
        "computed_at",
        "snapshot_computed_at",
        "snapshot_age_seconds",
    }

    def material(member):
        if isinstance(member, dict):
            return {key: material(item) for key, item in member.items() if key not in clocks}
        if isinstance(member, list):
            return [material(item) for item in member]
        return member

    return digest(material(value))


def financial_fingerprint(runtime, scope, db=None):
    from .cashflow import financial_fingerprint as native_state_fingerprint

    return native_state_fingerprint(runtime, scope, db=db)


def require_current(runtime, scope, base=None, db=None):
    runtime.workspaces.require_current(scope)
    if base is not None and not hmac.compare_digest(
        str(base), str(financial_fingerprint(runtime, scope, db))
    ):
        raise HTTPException(
            409, "The forecast changed. Refresh and draft a new proposal against its current version."
        )


def bind_drafts(db, scope, base):
    """Trusted session metadata cannot be supplied in a model tool argument."""
    db.info["local_proposal_review"] = {
        "workspace_id": scope["id"],
        "company_id": scope["realm_id"],
        "base_fingerprint": base,
        "requested_by_user_id": scope["user_id"],
    }


def install_draft_binding():
    from sqlalchemy import event
    from sqlalchemy.orm import object_session
    from app.models import ProposedAction

    # Importing the native executor registers the native dedup/provenance seam first.
    from app.agent.runtime import tool_executor  # noqa: F401
    from app.qbo_actions.proposal_fingerprint import compute_proposal_fingerprint

    if ProposedAction in _LISTENER_MODELS:
        return

    def stamp(_mapper, _connection, action):
        db = object_session(action)
        binding = db.info.get("local_proposal_review") if db is not None else None
        if not binding:
            return
        if action.company_id != binding["company_id"] or action.action_type not in ACTION_TO_TOOL:
            raise ValueError("A model attempted an action outside the local cash-flow surface")
        payload = dict(action.payload or {})
        payload["_local_review"] = dict(binding)
        action.payload = payload
        action.inputs_fingerprint = compute_proposal_fingerprint(action.action_type, payload)
        # Requesting a draft does not make the human its preparer.
        action.human_preparer_user_id = None
        action.last_human_editor_user_id = None

    event.listen(ProposedAction, "before_insert", stamp)
    _LISTENER_MODELS.add(ProposedAction)


def review_fingerprint(action):
    payload = dict(action.payload or {})
    binding = payload.pop("_local_review", {})
    return digest(
        {
            "id": action.id,
            "company_id": action.company_id,
            "action_type": action.action_type,
            "revision": action.revision,
            "payload": payload,
            "workspace_id": binding.get("workspace_id"),
            "base_fingerprint": binding.get("base_fingerprint"),
        }
    )


def _owned(db, scope, action_id):
    from app.models import ProposedAction

    row = (
        db.query(ProposedAction)
        .filter(ProposedAction.id == action_id, ProposedAction.company_id == scope["realm_id"])
        .first()
    )
    if row is None or row.action_type not in ACTION_TO_TOOL:
        raise HTTPException(404, "Cash-flow proposal not found in this workspace")
    binding = (row.payload or {}).get("_local_review", {})
    if binding.get("workspace_id") != scope["id"] or not binding.get("base_fingerprint"):
        raise HTTPException(409, "This proposal lacks a current local review binding. Draft it again.")
    return row


def _check_review(action, value):
    if not isinstance(value, str) or not hmac.compare_digest(review_fingerprint(action), value):
        raise HTTPException(409, "The proposal changed after review. Open its latest details.")


def _expired(action):
    if action.expires_at is None:
        return False
    expiry = action.expires_at
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    return expiry <= datetime.now(timezone.utc)


def serialize(action, scope, current):
    payload = dict(action.payload or {})
    binding = payload.pop("_local_review", {})
    preview = binding.get("preview") or {}
    return {
        "id": action.id,
        "action_type": action.action_type,
        "status": action.status,
        "revision": action.revision,
        "source": action.source,
        "summary": payload.get("summary") or action.reasoning or action.action_type,
        "payload": payload,
        "created_at": action.created_at,
        "reviewed_by": action.reviewed_by,
        "reviewed_at": action.reviewed_at,
        "execution_result": action.execution_result,
        "base_fingerprint": binding.get("base_fingerprint"),
        "current_fingerprint": current,
        "stale": binding.get("base_fingerprint") != current or _expired(action),
        "review_fingerprint": review_fingerprint(action),
        "preview_available": bool(preview and preview.get("base_fingerprint") == current),
        "approval_snapshots": binding.get("approval"),
        "review": {
            "before_state": payload.get("before_state"),
            "after_state": payload.get("after_state"),
            "approval_effect": payload.get("approval_effect"),
        },
    }


@contextmanager
def _scoped_session(runtime, scope):
    from app.tenant_rls import tenant_scope_live

    with runtime.session_factory() as db:
        runtime.user_for_scope(db, scope)
        with tenant_scope_live(db, org_id=scope["organization_id"], realm_id=scope["realm_id"]):
            yield db


def _forecast_comparison(before, after):
    keys = ("beginning_balances", "total_cash_in", "total_cash_out", "ending_balances")
    headers = before.get("week_headers")
    if not headers or headers != after.get("week_headers"):
        raise ValueError("Preview does not cover the same forecast weeks")
    lookback = before.get("lookback", 0)
    if (
        type(lookback) is not int
        or lookback < 0
        or lookback >= len(headers)
        or lookback != after.get("lookback", 0)
    ):
        raise ValueError("Preview must cover the same historical and forward forecast columns")
    for grid in (before, after):
        if any(not isinstance(grid.get(key), list) or len(grid[key]) != len(headers) for key in keys):
            raise ValueError("Preview is missing its complete financial grid")
    deltas = [
        float(Decimal(str(y)) - Decimal(str(x)))
        for x, y in zip(before["ending_balances"], after["ending_balances"])
    ]
    return {
        "week_headers": headers,
        "ending_balance_deltas": deltas,
        "ending_cash_delta": deltas[-1],
        "minimum_cash_delta": float(
            min(map(lambda x: Decimal(str(x)), after["ending_balances"][lookback:]))
            - min(map(lambda x: Decimal(str(x)), before["ending_balances"][lookback:]))
        ),
    }


def _clone_preview(runtime, scope, db, action):
    """Execute the real handler only on a SQLite backup, with cached/replay reads."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from app.assistant.handlers import TOOL_HANDLERS
    from app.models import ProposedAction
    from app.services.internal_action_execution import action_tool_input
    from .cashflow import cached_forecast
    from .connections import replay_source

    tool = ACTION_TO_TOOL[action.action_type]
    before = cached_forecast(runtime, scope, db=db)
    if not isinstance(before, dict):
        raise ValueError("A complete captured forecast is required for review")
    params = action_tool_input(action)
    if tool == "update_cashflow_cells":
        known = {
            "customer": {row["name"] for row in before.get("customers", [])},
            "vendor": {row["name"] for row in before.get("vendors", [])},
        }
        if not params.get("updates") or any(
            update.get("entity_name") not in known.get(update.get("entity_type"), set())
            or update.get("week_ending") not in before["week_headers"]
            for update in params["updates"]
        ):
            raise ValueError(
                "Cell changes must name an existing row and an exact Friday from the reviewed grid"
            )
    if tool == "layer_cashflow_schedule":
        if not 1 <= params.get("day_of_month", 1) <= 28:
            raise ValueError("Recurring schedule days must be in the native supported range of 1–28")
        if any(
            item.get("week_ending") not in before["week_headers"] for item in params.get("weekly_amounts", [])
        ):
            raise ValueError("Every explicit schedule occurrence must use a Friday in the reviewed grid")
    if tool == "spread_cashflow_balance" and (
        params.get("start_week_ending") and params["start_week_ending"] not in before["week_headers"]
    ):
        raise ValueError("The spread must start on a Friday in the reviewed grid")
    if tool == "categorize_cashflow_actuals" and params.get("items"):
        from app.models import CashForecastActual

        requested_ids = [item["id"] for item in params["items"]]
        owned_ids = {
            row.id
            for row in db.query(CashForecastActual)
            .filter(
                CashForecastActual.company_id == scope["realm_id"], CashForecastActual.id.in_(requested_ids)
            )
            .all()
        }
        if len(set(requested_ids)) != len(requested_ids) or set(requested_ids) != owned_ids:
            raise ValueError("Every actual transaction must exist exactly once in the active company")
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    db.connection().connection.driver_connection.backup(connection)
    connection.execute("PRAGMA foreign_keys=ON")
    engine = create_engine("sqlite://", creator=lambda: connection, poolclass=StaticPool)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    token = _NO_PROVIDER.set(True)
    try:
        with factory() as clone:
            runtime.user_for_scope(clone, scope)
            copied = clone.query(ProposedAction).filter_by(id=action.id, company_id=scope["realm_id"]).one()
            copied.reviewed_by = "local-preview-only"
            params = action_tool_input(copied)
            with replay_source(scope):
                result = TOOL_HANDLERS[tool](clone, scope["realm_id"], params)
                if (
                    not isinstance(result, str)
                    or result.startswith("Error:")
                    or "error(s)" in result
                    or result.startswith("No matching cash flow actuals were updated")
                ):
                    raise ValueError("The native handler could not preview the complete requested change")
                after = cached_forecast(runtime, scope, db=clone)
        return before, after, _forecast_comparison(before, after)
    finally:
        _NO_PROVIDER.reset(token)
        engine.dispose()


def register(app, runtime):
    install_draft_binding()
    install_execution_fence()
    router = APIRouter(prefix="/local/proposals", tags=["Local governed proposal review"])
    lock = getattr(runtime, "financial_lock", _FALLBACK_LOCK)

    @router.get("")
    def list_proposals(request: Request):
        from app.models import ProposedAction

        scope = runtime.require_scope(request)
        with lock, _scoped_session(runtime, scope) as db:
            current = financial_fingerprint(runtime, scope, db)
            rows = (
                db.query(ProposedAction)
                .filter(
                    ProposedAction.company_id == scope["realm_id"],
                    ProposedAction.action_type.in_(tuple(ACTION_TO_TOOL)),
                )
                .order_by(ProposedAction.id.desc())
                .limit(200)
                .all()
            )
            return {
                "proposals": [
                    serialize(row, scope, current)
                    for row in rows
                    if (row.payload or {}).get("_local_review", {}).get("workspace_id") == scope["id"]
                ]
            }

    @router.get("/{action_id}")
    def get_proposal(action_id: int, request: Request):
        scope = runtime.require_scope(request)
        with lock, _scoped_session(runtime, scope) as db:
            return serialize(_owned(db, scope, action_id), scope, financial_fingerprint(runtime, scope, db))

    @router.post("/{action_id}/preview")
    def preview(action_id: int, body: dict, request: Request):
        scope = runtime.require_scope(request)
        with lock, _scoped_session(runtime, scope) as db:
            action = _owned(db, scope, action_id)
            _check_review(action, body.get("review_fingerprint"))
            binding = dict(action.payload["_local_review"])
            require_current(runtime, scope, binding["base_fingerprint"], db)
            if action.status != "PENDING" or _expired(action):
                raise HTTPException(409, "Only a current pending proposal can be previewed")
            try:
                before, after, comparison = _clone_preview(runtime, scope, db, action)
                canonical({"before": before, "after": after, "comparison": comparison})
            except (ValueError, HTTPException) as exc:
                db.rollback()
                return {
                    "available": False,
                    "reason": str(exc.detail if isinstance(exc, HTTPException) else exc),
                    "review_fingerprint": review_fingerprint(action),
                }
            except Exception:
                db.rollback()
                return {
                    "available": False,
                    "reason": "A complete no-egress native financial preview is unavailable. Refresh captured source data or draft a supported cash-flow change.",
                    "review_fingerprint": review_fingerprint(action),
                }
            require_current(runtime, scope, binding["base_fingerprint"], db)
            token = secrets.token_urlsafe(32)
            fingerprint = review_fingerprint(action)
            binding["preview"] = {
                "token_hash": hashlib.sha256(token.encode()).hexdigest(),
                "review_fingerprint": fingerprint,
                "base_fingerprint": binding["base_fingerprint"],
                "before_hash": impact_digest(before),
                "after_hash": impact_digest(after),
                "comparison": comparison,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            action.payload = {**action.payload, "_local_review": binding}
            db.commit()
            return {
                "available": True,
                "before": before,
                "after": after,
                "comparison": comparison,
                "review_fingerprint": fingerprint,
                "preview_token": token,
            }

    @router.post("/{action_id}/approve")
    def approve(action_id: int, body: dict, request: Request):
        from app.qbo_actions.transitions import claim_action_transition
        from app.services.internal_action_execution import execute_internal_db_action_core
        from .cashflow import capture_export_snapshot

        scope = runtime.require_scope(request)
        with lock, _scoped_session(runtime, scope) as db:
            action = _owned(db, scope, action_id)
            _check_review(action, body.get("review_fingerprint"))
            binding = action.payload["_local_review"]
            require_current(runtime, scope, binding["base_fingerprint"], db)
            preview = binding.get("preview") or {}
            token = body.get("preview_token")
            if (
                not isinstance(token, str)
                or not token
                or not preview
                or preview.get("review_fingerprint") != review_fingerprint(action)
                or preview.get("base_fingerprint") != binding["base_fingerprint"]
                or not hmac.compare_digest(
                    str(preview.get("token_hash", "")), hashlib.sha256(token.encode()).hexdigest()
                )
            ):
                raise HTTPException(409, "Preview and review the exact current change before approving")
            if action.status != "PENDING" or _expired(action):
                raise HTTPException(409, "This proposal is no longer pending")
            # Replay the exact native calculation again before admitting a write.
            # Export envelopes freeze both sides even though PE's current cache
            # itself remains mutable source behavior.
            try:
                before, after, _comparison = _clone_preview(runtime, scope, db, action)
            except Exception:
                raise HTTPException(
                    409, "The reviewed impact is no longer reproducible. Generate a fresh preview."
                ) from None
            if impact_digest(before) != preview.get("before_hash") or impact_digest(after) != preview.get(
                "after_hash"
            ):
                raise HTTPException(
                    409, "The forecast impact changed after preview. Generate and review a fresh preview."
                )
            before_export = capture_export_snapshot(runtime, scope, db)
            user = runtime.user_for_scope(db, scope)
            if not claim_action_transition(
                db,
                action_id=action.id,
                from_status="PENDING",
                to_status="APPROVED",
                reviewed_by=user.email,
                expected_revision=action.revision,
                approver_user_id=user.id,
            ):
                raise HTTPException(409, "Another review already resolved this proposal")
            binding = dict(binding)
            binding["approval"] = {
                "before_export": before_export,
                "reviewed_fingerprint": preview["review_fingerprint"],
                "reviewed_by_user_id": user.id,
                "reviewed_at": datetime.now(timezone.utc).isoformat(),
            }
            action.payload = {**action.payload, "_local_review": binding}
            db.commit()
            db.refresh(action)
            execution_review = {
                "db": db,
                "tool": ACTION_TO_TOOL[action.action_type],
                "scope": scope,
                "runtime": runtime,
                "after_hash": preview["after_hash"],
            }
            execution_token = _EXECUTION_REVIEW.set(execution_review)
            try:
                outcome = execute_internal_db_action_core(
                    db, action, actor_email=user.email, failure_audit_source="local_cashflow_review"
                )
            finally:
                _EXECUTION_REVIEW.reset(execution_token)
            db.refresh(action)
            snapshot_error = None
            try:
                after_export = capture_export_snapshot(runtime, scope, db) if outcome.succeeded else None
            except Exception:
                db.rollback()
                after_export, snapshot_error = (
                    None,
                    "The change executed, but the resulting export capture needs review. Do not retry the financial action.",
                )
            binding = dict(action.payload["_local_review"])
            binding["approval"] = {
                **binding["approval"],
                "after_export": after_export,
                "executed": outcome.executed,
                "snapshot_error": snapshot_error,
                "financial_rollback_confirmed": execution_review.get("financial_rollback_confirmed", False),
            }
            action.payload = {**action.payload, "_local_review": binding}
            db.commit()
            db.refresh(action)
            return {
                "ok": bool(outcome.executed),
                "outcome": outcome.execution_status,
                "message": "The financial change was rolled back before commit because its execution could not reproduce the exact reviewed impact. Its admission record is preserved for review; do not retry the action."
                if execution_review.get("financial_rollback_confirmed")
                else outcome.message,
                "proposal": serialize(action, scope, financial_fingerprint(runtime, scope, db)),
            }

    @router.post("/{action_id}/reject")
    def reject(action_id: int, body: dict, request: Request):
        from app.qbo_actions.transitions import claim_action_transition

        scope = runtime.require_scope(request)
        with lock, _scoped_session(runtime, scope) as db:
            action = _owned(db, scope, action_id)
            _check_review(action, body.get("review_fingerprint"))
            user = runtime.user_for_scope(db, scope)
            if not claim_action_transition(
                db,
                action_id=action.id,
                from_status="PENDING",
                to_status="REJECTED",
                reviewed_by=user.email,
                expected_revision=action.revision,
            ):
                raise HTTPException(409, "This proposal is already resolved")
            db.commit()
            db.refresh(action)
            return {
                "ok": True,
                "proposal": serialize(action, scope, financial_fingerprint(runtime, scope, db)),
            }

    app.include_router(router)

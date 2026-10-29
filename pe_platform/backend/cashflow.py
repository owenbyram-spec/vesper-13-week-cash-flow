"""Mount the native forecast and exact-ID report route with local transport seams."""

from __future__ import annotations

import asyncio
import calendar
import copy
import hashlib
import json
import logging
from datetime import date, datetime, timezone
from decimal import Decimal
from functools import wraps

from fastapi import HTTPException

from .connections import install_native_read_seam, is_replaying, replay_source
from .security import validate_money_tree

_native_apply_live_layer = None
logger = logging.getLogger(__name__)


def _json(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    return str(value)


def _material(value):
    if isinstance(value, dict):
        return {
            key: _material(member)
            for key, member in value.items()
            if key not in {"Time", "computed_at", "captured_at", "created_at", "updated_at", "MetaData"}
        }
    if isinstance(value, list):
        return [_material(member) for member in value]
    return value


def financial_fingerprint(runtime, scope, db=None):
    """Hash material native inputs/state only: no network, refresh or export writes."""
    from app.models import (
        CashForecastActual,
        CashForecastOverride,
        CashForecastSnapshot,
        CashForecastWeekStatus,
    )
    from app.tenant_rls import tenant_scope

    own = db is None
    session = db or runtime.session_factory()
    try:
        with tenant_scope(org_id=scope["organization_id"], realm_id=scope["realm_id"]):
            material = {}
            for model in (
                CashForecastActual,
                CashForecastOverride,
                CashForecastSnapshot,
                CashForecastWeekStatus,
            ):
                rows = (
                    session.query(model)
                    .filter(model.company_id == scope["realm_id"])
                    .order_by(model.id)
                    .all()
                )
                material[model.__tablename__] = [
                    _material({column.name: getattr(row, column.name) for column in model.__table__.columns})
                    for row in rows
                ]
            source = runtime.workspaces.read_source(scope)
            base = source.get("last_served_payload", {})
            # Adding an authorised read-cache key is not a financial mutation.
            # Native serve-cache/actuals/overrides above contain calculation inputs;
            # retain the captured bank/control evidence separately, without traces.
            material["source"] = _material(
                {
                    "fixture": source.get("fixture"),
                    "bank_control": {
                        key: base.get(key)
                        for key in (
                            "bank_balance",
                            "bank_balance_as_of",
                            "bank_balance_source",
                            "anchor_diagnostics",
                            "reconciliation_delta",
                            "data_quality",
                        )
                    },
                }
            )
            material["scope"] = {key: scope[key] for key in ("id", "realm_id", "organization_id", "revision")}
            material["calendar_date"] = date.today().isoformat()
            return hashlib.sha256(
                json.dumps(
                    material, sort_keys=True, separators=(",", ":"), default=_json, allow_nan=False
                ).encode()
            ).hexdigest()
    finally:
        if own:
            session.close()


def cached_forecast(runtime, scope, db):
    """Re-run native overrides/actuals against captured evidence, never live QBO."""
    from app.models import CashForecastSnapshot
    from app.routers import cashflow_forecast as native
    from app.tenant_rls import tenant_scope

    if _native_apply_live_layer is None:
        raise RuntimeError("Register the native cash-flow adapter first.")
    with tenant_scope(org_id=scope["organization_id"], realm_id=scope["realm_id"]):
        snapshot = (
            db.query(CashForecastSnapshot)
            .filter(CashForecastSnapshot.company_id == scope["realm_id"])
            .first()
        )
        source = runtime.workspaces.read_source(scope)
        base = source.get("last_served_payload")
        if not snapshot or not base:
            raise HTTPException(
                409, "Load and review a complete forecast source before requesting a proposal preview."
            )
        lookback = int(base.get("lookback", 1))
        weeks = len(base.get("week_headers", [])) - lookback
        if [value.isoformat() for value in native._week_fridays(weeks, lookback=lookback)] != base.get(
            "week_headers"
        ):
            raise HTTPException(
                409,
                "The forecast calendar changed; refresh the current source before regenerating this proposal.",
            )
        with replay_source(scope):
            payload = _native_apply_live_layer(
                copy.deepcopy(snapshot.snapshot_data),
                float(base["bank_balance"]),
                scope["realm_id"],
                weeks,
                lookback,
                db,
                bank_balance_as_of=date.fromisoformat(base["bank_balance_as_of"]),
                bank_balance_source=base.get("bank_balance_source", "unknown"),
                anchor_diagnostics=copy.deepcopy(base.get("anchor_diagnostics")),
            )
        validate_money_tree(payload)
        computed = snapshot.computed_at
        if computed is not None and computed.tzinfo is None:
            computed = computed.replace(tzinfo=timezone.utc)
        payload.update(
            {
                "snapshot_state": base.get("snapshot_state", "ready"),
                "snapshot_computed_at": computed.isoformat() if computed else None,
                "snapshot_age_seconds": base.get("snapshot_age_seconds", 0),
            }
        )
        if payload.get("data_quality", {}).get("is_partial"):
            raise HTTPException(
                409, "Captured source evidence is incomplete; review and refresh before proposing changes."
            )
        return payload


def capture_export_snapshot(runtime, scope, db=None):
    """Durable native immutable envelope for the captured reviewed financial state."""
    from app.cashflow.export_snapshots import capture_forecast_export, content_from_weekly_payload

    own = db is None
    session = db or runtime.session_factory()
    try:
        payload = cached_forecast(runtime, scope, session)
        content = content_from_weekly_payload(
            payload, company_id=scope["realm_id"], company_display_name=scope["name"]
        )
        export = capture_forecast_export(
            content, organization_id=scope["organization_id"], session_factory=runtime.session_factory
        )
        return export.model_dump(mode="json")
    finally:
        if own:
            session.close()


def register(app, runtime):
    global _native_apply_live_layer
    from app.routers import cashflow_forecast as native
    from app.routers.reporting import download_cashflow_forecast_export
    from app.tenant_rls import tenant_scope

    if getattr(runtime, "_native_cashflow_adapters_installed", False):
        app.include_router(native.router)
        app.add_api_route(
            "/reporting/cashflow-forecast/exports/{export_id}/download",
            runtime._local_download_handler,
            methods=["GET"],
        )
        return
    install_native_read_seam(runtime)
    _install_finite_spread_adapter(native, runtime)
    _native_apply_live_layer = native._apply_live_layer
    original_attach_export = native._attach_export_snapshot_best_effort

    @wraps(original_attach_export)
    def attach_export(db, company_id, payload):
        scope = runtime.workspaces.active()
        if scope and scope["realm_id"] == company_id and is_replaying(scope):
            # A clone preview must never persist through the source capture
            # service's independently owned, real-DB transaction.
            return {
                **payload,
                "export_id": None,
                "export_company_id": company_id,
                "export_schema_version": native.FORECAST_EXPORT_SCHEMA_VERSION,
                "export_capture_state": "preview_only",
                "export_error": None,
            }
        return original_attach_export(db, company_id, payload)

    native._attach_export_snapshot_best_effort = attach_export

    @wraps(_native_apply_live_layer)
    def capture_live_layer(*args, **kwargs):
        payload = _native_apply_live_layer(*args, **kwargs)
        validate_money_tree(payload)
        realm = kwargs.get("company_id", args[2] if len(args) > 2 else None)
        scope = runtime.workspaces.active()
        if scope and realm == scope["realm_id"] and not is_replaying(scope):
            source = runtime.workspaces.read_source(scope)
            source["last_served_payload"] = _plain_payload(payload)
            runtime.workspaces.write_source(scope, source)
        return payload

    native._apply_live_layer = capture_live_layer

    def local_enqueue(company_id, weeks, lookback):
        scope = runtime.workspaces.active()
        if not scope or scope["realm_id"] != company_id:
            return {
                "error_code": "local_scope_changed",
                "user_message": "Select the intended workspace before refreshing.",
                "retryable": True,
            }
        # The local queue uses the same native worker function, without Redis/Celery.
        if runtime.security.loop is None:
            return {
                "error_code": "local_queue_not_ready",
                "user_message": "Use explicit Refresh while the local application starts.",
                "retryable": True,
            }

        async def job():
            async with runtime.security.operation_lock:

                def build():
                    from app.tasks.cash_forecast_refresh import refresh_forecast_snapshot_for_realm

                    with runtime.financial_lock:
                        runtime.workspaces.require_current(scope)
                        with (
                            tenant_scope(org_id=scope["organization_id"], realm_id=scope["realm_id"]),
                            runtime.session_factory() as db,
                        ):
                            return refresh_forecast_snapshot_for_realm(
                                db, company_id=company_id, weeks=weeks, lookback=lookback
                            )

                try:
                    await asyncio.to_thread(build)
                except Exception as exc:
                    # A failed acquisition never replaces the source cache with zeros.
                    # Native GET remains building/stale; explicit Refresh exposes its error.
                    logger.warning(
                        "Local forecast background acquisition failed (%s); use explicit Refresh to review the source error.",
                        type(exc).__name__,
                    )
                    return

        asyncio.run_coroutine_threadsafe(job(), runtime.security.loop)
        return None

    native._enqueue_snapshot_refresh = local_enqueue

    from app.services import ar_ap_summary

    def local_ar_ap_enqueue(company_id, period):
        scope = runtime.workspaces.active()
        if not scope or scope["realm_id"] != company_id:
            return {
                "error_code": "local_scope_changed",
                "user_message": "Select the intended workspace before refreshing.",
                "retryable": True,
            }
        if runtime.security.loop is None:
            return {
                "error_code": "local_queue_not_ready",
                "user_message": "Use explicit AR/AP refresh while the local application starts.",
                "retryable": True,
            }

        async def job():
            async with runtime.security.operation_lock:

                def build():
                    with runtime.financial_lock:
                        runtime.workspaces.require_current(scope)
                        with (
                            tenant_scope(org_id=scope["organization_id"], realm_id=scope["realm_id"]),
                            runtime.session_factory() as db,
                        ):
                            payload = ar_ap_summary.compute_summary_payload(db, company_id)
                            validate_money_tree(payload)
                            ar_ap_summary.upsert_snapshot(db, company_id, period, payload)
                            db.commit()

                try:
                    await asyncio.to_thread(build)
                except Exception as exc:
                    logger.warning(
                        "Local AR/AP background acquisition failed (%s); use explicit refresh to review the source error.",
                        type(exc).__name__,
                    )

        asyncio.run_coroutine_threadsafe(job(), runtime.security.loop)
        return None

    ar_ap_summary._enqueue_warm = local_ar_ap_enqueue

    def locked(function):
        @wraps(function)
        def call(*args, **kwargs):
            with runtime.financial_lock:
                result = function(*args, **kwargs)
                if isinstance(result, dict):
                    validate_money_tree(result)
                return result

        return call

    for route in native.router.routes:
        if hasattr(route, "dependant"):
            route.endpoint = locked(route.endpoint)
            route.dependant.call = route.endpoint
    app.include_router(native.router)
    runtime._local_download_handler = locked(download_cashflow_forecast_export)
    app.add_api_route(
        "/reporting/cashflow-forecast/exports/{export_id}/download",
        runtime._local_download_handler,
        methods=["GET"],
    )
    runtime._native_cashflow_adapters_installed = True


def _plain_payload(payload):
    return json.loads(json.dumps(payload, default=_json, allow_nan=False))


def _install_finite_spread_adapter(native, runtime):
    """Keep finite balances finite without changing native recurring run rates.

    Upstream writes a monthly-average sentinel for every finite spread, then its
    live layer extrapolates additional months. This local correction reuses the
    native cent split, but stores zero run rate and only explicit occurrences.
    Monthly weekend dates advance to their next Friday; periods outside the
    reviewed window are refused instead of folded into the first bucket.
    """
    from app.models import CashForecastOverride, CashForecastSnapshot
    from .security import validate_amount

    original = native.apply_spread_balance

    class DeferredCommitSession:
        def __init__(self, db):
            self.db = db

        def __getattr__(self, name):
            return getattr(self.db, name)

        def commit(self):
            # The source helper commits internally. Keep its split and local
            # sentinel correction in one real guarded transaction.
            self.db.flush()

    @wraps(original)
    def finite_spread(db, **kwargs):
        with runtime.financial_lock:
            company_id = str(kwargs.get("company_id", ""))
            name = str(kwargs.get("entity_name") or "").strip()
            entity_type = str(kwargs.get("entity_type") or "").strip()
            cadence = str(kwargs.get("cadence") or "").strip().lower()
            periods = kwargs.get("periods")
            day = 15 if kwargs.get("day_of_month") is None else kwargs["day_of_month"]
            if not name or len(name) > 200 or entity_type not in {"customer", "vendor"}:
                raise HTTPException(400, "Use a distinct spread row name and customer or vendor type.")
            if (
                cadence not in {"weekly", "monthly"}
                or isinstance(periods, bool)
                or not isinstance(periods, int)
                or not 1 <= periods <= 13
            ):
                raise HTTPException(400, "Choose a finite weekly or monthly spread of 1–13 periods.")
            if isinstance(day, bool) or not isinstance(day, int) or not 1 <= day <= 28:
                raise HTTPException(400, "A monthly spread day must be an integer from 1 through 28.")
            validate_amount(kwargs.get("total_amount"))
            fridays = native._week_fridays(13, 0)
            start = kwargs.get("start_week_ending") or fridays[0]
            if start not in fridays:
                raise HTTPException(400, "The spread must start on a Friday in the current forecast window.")
            if cadence == "weekly":
                target_weeks = fridays[fridays.index(start) : fridays.index(start) + periods]
                if len(target_weeks) != periods:
                    raise HTTPException(400, "The finite spread extends beyond the current forecast window.")
            else:
                target_weeks = []
                for offset in range(periods):
                    month_index = start.year * 12 + start.month - 1 + offset
                    year, month = divmod(month_index, 12)
                    due = date(year, month + 1, min(day, calendar.monthrange(year, month + 1)[1]))
                    target = max(start, native._next_friday(due)) if offset == 0 else native._next_friday(due)
                    if target not in fridays:
                        raise HTTPException(
                            400, "The finite monthly spread extends beyond the current forecast window."
                        )
                    target_weeks.append(target)
            existing = (
                db.query(CashForecastOverride)
                .filter(
                    CashForecastOverride.company_id == company_id,
                    CashForecastOverride.entity_name == name,
                    CashForecastOverride.entity_type == entity_type,
                )
                .first()
            )
            snapshot = (
                db.query(CashForecastSnapshot).filter(CashForecastSnapshot.company_id == company_id).first()
            )
            source_rows = (
                (snapshot.snapshot_data or {}).get(
                    "customer_rows" if entity_type == "customer" else "vendor_rows", []
                )
                if snapshot
                else []
            )
            if existing or any(row.get("name") == name for row in source_rows):
                raise HTTPException(
                    409,
                    "That row already contains source or manual assumptions. Use a new spread row name to preserve them.",
                )
            native_kwargs = {
                **kwargs,
                "entity_name": name,
                "entity_type": entity_type,
                "cadence": "weekly",
                "periods": periods,
                "day_of_month": day,
                "start_week_ending": start,
                "note": kwargs.get("note") or "Manual finite spread",
            }
            try:
                result = original(DeferredCommitSession(db), **native_kwargs)
                amounts = [item["amount"] for item in result["weekly_amounts"]]
                # Replace only the just-created helper buckets, not other rows.
                if cadence == "monthly":
                    db.query(CashForecastOverride).filter(
                        CashForecastOverride.company_id == company_id,
                        CashForecastOverride.entity_name == name,
                        CashForecastOverride.entity_type == entity_type,
                        CashForecastOverride.week_ending.in_(
                            [date.fromisoformat(item["week_ending"]) for item in result["weekly_amounts"]]
                        ),
                    ).delete(synchronize_session=False)
                    for week, amount in zip(target_weeks, amounts):
                        db.add(
                            CashForecastOverride(
                                company_id=company_id,
                                entity_name=name,
                                entity_type=entity_type,
                                week_ending=week,
                                override_amount=amount,
                                note=native_kwargs["note"],
                                created_by="cashflow_spread",
                            )
                        )
                average = (
                    db.query(CashForecastOverride)
                    .filter(
                        CashForecastOverride.company_id == company_id,
                        CashForecastOverride.entity_name == name,
                        CashForecastOverride.entity_type == entity_type,
                        CashForecastOverride.week_ending == native.AVG_SENTINEL,
                    )
                    .one()
                )
                average.override_amount = 0
                result.update(
                    {
                        "cadence": cadence,
                        "avg_amount": 0.0,
                        "weekly_amounts": [
                            {"week_ending": week.isoformat(), "amount": amount}
                            for week, amount in zip(target_weeks, amounts)
                        ],
                        "schedule_kind": "finite_balance_spread",
                    }
                )
                validate_money_tree(result)
                db.commit()
                return result
            except Exception:
                db.rollback()
                raise

    native.apply_spread_balance = finite_spread

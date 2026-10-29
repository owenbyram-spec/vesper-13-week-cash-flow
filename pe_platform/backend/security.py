"""Loopback sessions, CSRF and displayed-workspace binding for native routes."""

from __future__ import annotations

import asyncio
import json
import math
import secrets
from decimal import Decimal, InvalidOperation

from fastapi import HTTPException
from starlette.responses import JSONResponse

MAX_CENTS = 99_999_999_999_999
_MONEY_FIELDS = {
    "amount",
    "override_amount",
    "total_amount",
    "avg_amount",
    "effective_avg",
    "monthly_amount",
    "min_cash_floor",
    "beginning_balance",
    "bank_balance",
    "balance",
    "annual_revenue",
    "annual_cogs",
    "net_position",
    "total",
}
_MONEY_ARRAYS = {
    "amounts",
    "weekly_amounts",
    "total_cash_in",
    "total_cash_out",
    "beginning_balances",
    "ending_balances",
    "other_cash_in",
    "other_cash_out",
    "net_cash_flow",
}


def validate_amount(value):
    if isinstance(value, bool) or value is None:
        raise HTTPException(400, "Financial amounts must be finite, representable cents.")
    try:
        amount = Decimal(str(value))
        cents = amount * 100
        if not amount.is_finite() or cents != cents.to_integral_value() or abs(cents) > MAX_CENTS:
            raise ValueError()
        number = float(amount)
        if not math.isfinite(number) or Decimal(str(number)) != amount:
            raise ValueError()
    except (InvalidOperation, TypeError, ValueError, OverflowError):
        raise HTTPException(
            400, "Use finite whole cents within the native grid's safe numeric range."
        ) from None


def validate_money_tree(value, parent=""):
    if isinstance(value, dict):
        for key, member in value.items():
            if (
                member is not None
                and key in {"periods", "day_of_month"}
                and (isinstance(member, bool) or not isinstance(member, int))
            ):
                raise HTTPException(400, "Schedule periods and days must be whole integers.")
            if key in _MONEY_FIELDS and member is not None:
                validate_amount(member)
            elif key in _MONEY_ARRAYS and isinstance(member, list):
                for amount in member:
                    if isinstance(amount, (dict, list)):
                        validate_money_tree(amount, key)
                    elif amount is not None:
                        validate_amount(amount)
            else:
                validate_money_tree(member, key)
    elif isinstance(value, list):
        for member in value:
            validate_money_tree(member, parent)


class LocalSecurity:
    def __init__(self, runtime, port, frontend_origin=None, testing=False):
        self.runtime = runtime
        self.cookie_name = f"vesper_pe_session_{port}"
        self.session_token = secrets.token_urlsafe(32)
        self.csrf_token = secrets.token_urlsafe(32)
        self.hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        self.origins = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}
        if frontend_origin:
            self.origins.add(frontend_origin.rstrip("/"))
        if testing:
            self.hosts.add("testserver")
            self.origins.add("http://testserver")
        self.operation_lock = asyncio.Lock()
        self.loop = None

    def require_scope(self, request):
        active = self.runtime.workspaces.active()
        if not active:
            raise HTTPException(409, "Create or select a local workspace first.")
        if request.headers.get("x-vesper-workspace") != active["id"] or request.headers.get(
            "x-vesper-revision"
        ) != str(active["revision"]):
            raise HTTPException(409, "The displayed workspace is stale; reload before continuing.")
        request.state.workspace = active
        return active


class LocalSecurityMiddleware:
    def __init__(self, app, runtime):
        self.app, self.runtime = app, runtime

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        from starlette.requests import Request

        security = self.runtime.security
        security.loop = asyncio.get_running_loop()
        request = Request(scope)
        try:
            if request.headers.get("host", "").lower() not in security.hosts:
                raise HTTPException(403, "Use this local application's loopback address.")
            origin = request.headers.get("origin")
            if origin and origin.rstrip("/") not in security.origins:
                raise HTTPException(403, "This origin is not authorised for the local application.")
            if request.headers.get("sec-fetch-site") == "cross-site":
                raise HTTPException(403, "Cross-site local requests are refused.")
            bootstrap = request.url.path == "/local/bootstrap" and request.method == "GET"
            if not bootstrap:
                cookie = request.cookies.get(security.cookie_name, "")
                if not secrets.compare_digest(cookie, security.session_token):
                    raise HTTPException(401, "Open the local application to establish its session.")
            mutation_get = request.url.path == "/cashflow-forecast/actuals/pull"
            if request.method not in {"GET", "HEAD", "OPTIONS"} or mutation_get:
                if not secrets.compare_digest(request.headers.get("x-csrf-token", ""), security.csrf_token):
                    raise HTTPException(403, "The local CSRF token is missing or stale.")
            async with security.operation_lock:
                path = request.url.path
                exempt = bootstrap or path == "/local/workspaces/select"
                initial_create = (
                    path in {"/local/workspaces", "/local/demo"} and self.runtime.workspaces.active() is None
                )
                active = None if exempt or initial_create else security.require_scope(request)
                messages = []
                if request.method not in {"GET", "HEAD", "OPTIONS"}:
                    size = 0
                    while True:
                        message = await receive()
                        messages.append(message)
                        size += len(message.get("body", b""))
                        if size > 2_000_000:
                            raise HTTPException(413, "Local request exceeds its size limit.")
                        if not message.get("more_body", False):
                            break
                    body = b"".join(message.get("body", b"") for message in messages)
                    if body and "application/json" in request.headers.get("content-type", ""):
                        try:
                            parsed = json.loads(body)
                        except (ValueError, UnicodeDecodeError):
                            raise HTTPException(400, "Invalid JSON request.") from None
                        if active and isinstance(parsed, dict) and path != "/local/connections/qbo":
                            for field in ("company_id", "realm_id"):
                                if field in parsed and str(parsed[field]) != active["realm_id"]:
                                    raise HTTPException(
                                        403, "Request company differs from the displayed workspace."
                                    )
                        validate_money_tree(parsed)
                if active:
                    for key in ("company_id", "realm_id", "expected_company_id"):
                        if request.query_params.get(key) and request.query_params[key] != active["realm_id"]:
                            raise HTTPException(403, "Request company differs from the displayed workspace.")

                async def replay_receive():
                    return messages.pop(0) if messages else await receive()

                from app.tenant_rls import tenant_scope

                context = (
                    {}
                    if active is None
                    else {"org_id": active["organization_id"], "realm_id": active["realm_id"]}
                )
                with tenant_scope(**context):
                    await self.app(scope, replay_receive, send)
        except HTTPException as exc:
            await JSONResponse({"detail": exc.detail}, status_code=exc.status_code)(scope, receive, send)

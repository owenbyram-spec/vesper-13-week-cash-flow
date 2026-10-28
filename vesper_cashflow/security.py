"""Per-launch loopback session and cross-site request protection."""

from __future__ import annotations

import secrets
from urllib.parse import urlsplit

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse


class LocalSecurityMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, *, port: int, testing: bool = False):
        super().__init__(app)
        self.port = port
        self.testing = testing
        self.session = secrets.token_urlsafe(32)
        self.csrf_token = secrets.token_urlsafe(32)
        # Each app/port has a separate browser session even on the same host.
        self.cookie_name = f"vesper_session_{port}"

    def _valid_host(self, value: str) -> bool:
        allowed = {f"127.0.0.1:{self.port}"}
        if self.port == 80:
            allowed.add("127.0.0.1")
        if self.testing:
            allowed.add("testserver")
        return value.lower() in allowed

    def _valid_origin(self, value: str) -> bool:
        try:
            parsed = urlsplit(value)
            return (
                parsed.scheme == "http"
                and not parsed.username
                and not parsed.password
                and parsed.path == ""
                and not parsed.query
                and not parsed.fragment
                and self._valid_host(parsed.netloc)
            )
        except ValueError:
            return False

    async def dispatch(self, request: Request, call_next):
        def denied(message):
            return JSONResponse({"detail": message}, status_code=403)

        if not self._valid_host(request.headers.get("host", "")):
            return denied("Open the app at its displayed 127.0.0.1 address.")
        origin = request.headers.get("origin")
        if origin is not None and not self._valid_origin(origin):
            return denied("Cross-site requests are not allowed.")
        if request.headers.get("sec-fetch-site") in {"cross-site", "same-site"}:
            return denied("Use the app's local browser tab.")
        is_api = request.url.path == "/api" or request.url.path.startswith("/api/")
        if is_api:
            if not secrets.compare_digest(
                request.cookies.get(self.cookie_name, ""), self.session
            ):
                return denied("Open the local app before using its API.")
            if request.method not in {"GET", "HEAD", "OPTIONS"}:
                if not origin or not secrets.compare_digest(
                    request.headers.get("x-vesper-csrf", ""), self.csrf_token
                ):
                    return denied("Refresh the app and retry this action.")
            request.state.vesper_csrf = self.csrf_token
        response = await call_next(request)
        if request.method == "GET" and request.url.path == "/":
            response.set_cookie(
                self.cookie_name,
                self.session,
                httponly=True,
                samesite="strict",
                path="/",
            )
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        # The packaged UI needs no external script or frame origin.
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        return response

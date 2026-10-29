"""
app/services/sentry_scrub.py
────────────────────────────
The Sentry scrub seam (Wave C / AC-C1, DIM-026). Vesper Bot is financial /
PHI-adjacent: no QBO/Stytch token or financial identifier may leave the app
inside a Sentry event. The app already owns a battle-tested redactor
(`redact_to_string`) used at ~30 log sites, but it was NOT on the Sentry path —
auto-instrumented exceptions bypassed it. This module bridges that gap; its hooks
are wired into both Sentry init sites (app/main.py and app/celery_app.py).

Error events pass ``sentry_before_send``: the message, exception values,
breadcrumbs, request URL, body and auth headers are redacted, the query
string and cookies are dropped, and stack-frame local variables are dropped. Local
variables are also never collected (``include_local_variables=False``): the
SDK's own denylist matches exact top-level names only, so a nested
``data["refresh_token"]`` or ``headers["Authorization"]`` would pass it.
Performance transactions never reach ``before_send``; they pass
``sentry_before_send_transaction``, which applies the same request scrub and
also reduces span data to a non-sensitive allowlist.
"""
import re
from urllib.parse import urlsplit, urlunsplit

from .redaction import redact_sensitive, redact_to_string

# Request headers that carry credentials/session material — dropped wholesale.
# The BFF's attested client IP is the end user's address (the SDK's own PII
# filter only knows X-Forwarded-For / X-Real-IP), and its signature is a
# replayable credential for that address's rate-limit bucket.
_SENSITIVE_HEADERS = {
    "authorization",
    "cookie",
    "set-cookie",
    "x-csrf-token",
    "x-stytch-member-session",
    "x-stytch-session",
    "x-vesper-bot-client-ip",
    "x-vesper-bot-client-ip-signature",
}

# A URL path segment survives only when it is plainly a word (lowercase, no
# digits, short): ids, realm ids, approval/reset tokens and opaque keys are all
# replaced. Mixed or upper case is replaced too, because a short random token
# can be all letters but is almost never all lowercase.
_SAFE_PATH_SEGMENT_RE = re.compile(r"[a-z][a-z_.-]{0,39}")
_URL_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s\"'<>]+")
_REDACTED_SEGMENT = "[redacted]"

# Span / trace-context data keys that cannot carry URLs, queries, SQL
# parameters or payloads. Everything else is dropped from transactions.
_SAFE_SPAN_DATA_KEYS = frozenset({
    "thread.id",
    "thread.name",
    "http.method",
    "http.request.method",
    "http.status_code",
    "http.response.status_code",
    "db.system",
    "db.operation",
})


def _scrub_path(path: str) -> str:
    return "/".join(
        segment if not segment or _SAFE_PATH_SEGMENT_RE.fullmatch(segment) else _REDACTED_SEGMENT
        for segment in path.split("/")
    )


def scrub_url(url: str, *, route_template: str | None = None) -> str:
    """Return ``url`` without credentials, query, fragment or identifier-like path segments.

    When the request matched a route, its template (``/actions/approve-token/{token}``)
    replaces the concrete path, so no path parameter value survives.
    """
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return _REDACTED_SEGMENT
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    netloc = f"{host}:{port}" if host and port else host
    path = route_template if route_template else _scrub_path(parts.path)
    return urlunsplit((parts.scheme, netloc, path, "", ""))


def _scrub_text_urls(text: str) -> str:
    return _URL_RE.sub(lambda match: scrub_url(match.group(0)), text)


def _route_template(event: dict) -> str | None:
    """The matched route template, when the integration recorded one."""
    info = event.get("transaction_info") or {}
    name = event.get("transaction")
    if info.get("source") == "route" and isinstance(name, str) and name.startswith("/"):
        return name
    return None


def _strip_frame_vars(event: dict) -> None:
    stacktraces = [event.get("stacktrace")]
    for key in ("exception", "threads"):
        container = event.get(key) or {}
        for value in (container.get("values") if isinstance(container, dict) else None) or []:
            if isinstance(value, dict):
                stacktraces.append(value.get("stacktrace"))
    for stacktrace in stacktraces:
        if isinstance(stacktrace, dict):
            for frame in stacktrace.get("frames") or []:
                if isinstance(frame, dict):
                    frame.pop("vars", None)


def _scrub_request(event: dict) -> None:
    req = event.get("request")
    if not isinstance(req, dict):
        return
    if req.get("url"):
        req["url"] = scrub_url(str(req["url"]), route_template=_route_template(event))
    # Query strings carry OAuth codes/state, realm ids and ad-hoc tokens under
    # names no denylist can anticipate; they are never sent.
    req.pop("query_string", None)
    if req.get("data") is not None:
        req["data"] = redact_to_string(req["data"], max_length=1000)
    req.pop("cookies", None)
    req.pop("env", None)
    headers = req.get("headers")
    if isinstance(headers, dict):
        req["headers"] = {
            k: ("[redacted]" if k.lower() in _SENSITIVE_HEADERS else v)
            for k, v in headers.items()
        }


def _scrub_breadcrumbs(event: dict) -> None:
    breadcrumbs = event.get("breadcrumbs")
    crumbs = breadcrumbs.get("values") if isinstance(breadcrumbs, dict) else breadcrumbs
    for crumb in crumbs or []:
        if not isinstance(crumb, dict):
            continue
        if crumb.get("message"):
            crumb["message"] = redact_to_string(crumb["message"], max_length=500)
        data = crumb.get("data")
        if isinstance(data, dict):
            # HTTP-client breadcrumbs carry the outbound URL and its query separately.
            data.pop("http.query", None)
            data.pop("http.fragment", None)
            if isinstance(data.get("url"), str):
                data["url"] = scrub_url(data["url"])


def _scrub_span(span: dict) -> None:
    data = span.get("data")
    if isinstance(data, dict):
        span["data"] = {k: v for k, v in data.items() if k in _SAFE_SPAN_DATA_KEYS}
    description = span.get("description")
    if isinstance(description, str) and description:
        span["description"] = redact_sensitive(_scrub_text_urls(description), max_length=500)


def sentry_before_send(event: dict, hint: dict | None = None) -> dict:
    """Scrub a Sentry error event in place before it leaves the process."""
    # 1. Top-level message (logger-captured events).
    if event.get("message"):
        event["message"] = redact_to_string(event["message"], max_length=1000)

    # 2. Exception values — where QBO/Stytch tokens surface in error text.
    exception = event.get("exception") or {}
    for exc in exception.get("values") or []:
        if isinstance(exc, dict) and "value" in exc and exc["value"]:
            exc["value"] = redact_to_string(exc["value"], max_length=1000)

    # 3. Stack-frame local variables (refresh tokens, decrypted secrets, request
    #    dicts) — never sent, even if a future SDK default collects them.
    _strip_frame_vars(event)

    # 4. Breadcrumb messages and HTTP-client breadcrumb URLs.
    _scrub_breadcrumbs(event)

    # 5. Request payload — URL path parameters / query string / body carry tokens
    #    and realm ids; cookies + auth headers carry the session token.
    _scrub_request(event)

    return event


def sentry_before_send_transaction(event: dict, hint: dict | None = None) -> dict:
    """Scrub a performance transaction (these never pass ``before_send``)."""
    sentry_before_send(event, hint)
    info = event.get("transaction_info") or {}
    if info.get("source") == "url" and isinstance(event.get("transaction"), str):
        event["transaction"] = _scrub_path(event["transaction"])
    for span in event.get("spans") or []:
        if isinstance(span, dict):
            _scrub_span(span)
    trace = (event.get("contexts") or {}).get("trace")
    if isinstance(trace, dict):
        _scrub_span(trace)
    return event

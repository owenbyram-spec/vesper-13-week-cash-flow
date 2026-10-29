"""Bounded read transport. The injected sender alone owns retained credentials.

No host, SQL, arbitrary URL or financial write is accepted here. These objects
are source contracts, never authority to widen authenticated company context.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from email.utils import parsedate_to_datetime
import json
import math
import re
import time
from typing import Any, Callable, Mapping
from types import MappingProxyType

from .contracts import (BudgetExceeded, SourceContractError, SourceScope,
                        canonical_digest, exact_decimal)


@dataclass(frozen=True)
class ReadLimits:
    seconds: int = 600
    requests: int = 1000
    pages: int = 1000
    rows: int = 100000
    response_bytes: int = 8 * 1024 * 1024
    retained_bytes: int = 64 * 1024 * 1024
    diagnostic_bytes: int = 512 * 1024
    depth: int = 64
    attempts_per_operation: int = 3

    def __post_init__(self) -> None:
        ceilings = (600, 1000, 1000, 100000, 8 * 1024 * 1024,
                    64 * 1024 * 1024, 512 * 1024, 64, 3)
        values = (self.seconds, self.requests, self.pages, self.rows, self.response_bytes,
                  self.retained_bytes, self.diagnostic_bytes, self.depth, self.attempts_per_operation)
        if any(type(value) is not int or value <= 0 or value > cap for value, cap in zip(values, ceilings)):
            raise SourceContractError("limits must be positive integers at or below reviewed ceilings")


@dataclass
class ReadBudget:
    deadline: float
    limits: ReadLimits = field(default_factory=ReadLimits)
    clock: Callable[[], float] = time.monotonic
    requests: int = 0
    pages: int = 0
    rows: int = 0
    retained_bytes: int = 0
    diagnostic_bytes: int = 0
    attempts: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        now = self.clock()
        if not isinstance(self.deadline, (int, float)) or isinstance(self.deadline, bool) or not math.isfinite(self.deadline):
            raise SourceContractError("finite absolute monotonic deadline required")
        if self.deadline <= now or self.deadline - now > self.limits.seconds:
            raise BudgetExceeded("partition deadline is expired or exceeds its original ceiling")

    @classmethod
    def start(cls, *, limits: ReadLimits | None = None, clock: Callable[[], float] = time.monotonic) -> ReadBudget:
        selected = limits or ReadLimits()
        return cls(clock() + selected.seconds, selected, clock)

    def remaining(self) -> float:
        remaining = self.deadline - self.clock()
        if remaining <= 0:
            raise BudgetExceeded("absolute extraction deadline exhausted")
        return remaining

    def timeout(self, cap: float = 120.0) -> float:
        if not isinstance(cap, (int, float)) or isinstance(cap, bool) or not math.isfinite(cap) or cap <= 0:
            raise SourceContractError("positive timeout ceiling required")
        return min(cap, self.remaining())

    def admit_request(self, operation_id: str) -> None:
        self.remaining()
        if not isinstance(operation_id, str) or not operation_id or len(operation_id) > 256:
            raise SourceContractError("bounded stable operation key required")
        attempts = self.attempts.get(operation_id, 0)
        if self.requests >= self.limits.requests or attempts >= self.limits.attempts_per_operation:
            raise BudgetExceeded("physical send budget exhausted, including refreshes and retries")
        self.requests += 1
        self.attempts[operation_id] = attempts + 1

    def admit_wait(self, seconds: float) -> None:
        if not isinstance(seconds, (int, float)) or isinstance(seconds, bool) or not math.isfinite(seconds) or seconds < 0:
            raise SourceContractError("finite nonnegative wait required")
        if seconds >= self.remaining():
            raise BudgetExceeded("wait would consume remaining absolute deadline")

    def wait(self, seconds: float, *, sleep: Callable[[float], None] = time.sleep) -> None:
        self.admit_wait(seconds)
        sleep(seconds)
        self.remaining()

    def account_response(self, raw: bytes) -> None:
        self.remaining()
        if type(raw) is not bytes:
            raise SourceContractError("raw bytes required before JSON conversion")
        if len(raw) > self.limits.response_bytes or self.retained_bytes + len(raw) > self.limits.retained_bytes:
            raise BudgetExceeded("source response/retained bytes exhausted")
        self.retained_bytes += len(raw)

    def account_page(self, rows: int) -> None:
        self.remaining()
        if type(rows) is not int or rows < 0 or self.pages >= self.limits.pages or self.rows + rows > self.limits.rows:
            raise BudgetExceeded("page/normalized-row budget exhausted")
        self.pages += 1
        self.rows += rows

    def account_diagnostics(self, size: int) -> None:
        self.remaining()
        if type(size) is not int or size < 0 or self.diagnostic_bytes + size > self.limits.diagnostic_bytes:
            raise BudgetExceeded("diagnostic byte budget exhausted")
        self.diagnostic_bytes += size


def _pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in items:
        if key in result:
            raise SourceContractError("duplicate JSON key")
        result[key] = value
    return result


def _constant(_: str) -> None:
    raise SourceContractError("nonfinite JSON number")


def _json_integer(value: str) -> int:
    if len(value) > 128:
        raise SourceContractError("oversized JSON integer")
    return int(value)


def strict_json_loads(raw: bytes, *, maximum_bytes: int = 8 * 1024 * 1024, maximum_depth: int = 64) -> Any:
    if type(maximum_bytes) is not int or not 0 < maximum_bytes <= 8 * 1024 * 1024 or type(maximum_depth) is not int or not 0 < maximum_depth <= 64:
        raise SourceContractError("JSON limits cannot widen reviewed ceilings")
    if type(raw) is not bytes or len(raw) > maximum_bytes:
        raise SourceContractError("raw JSON bytes exceed the response contract")
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise SourceContractError("provider response is not strict UTF-8") from exc
    # Bound nesting before the recursive JSON decoder allocates nested objects.
    depth, quoted, escaped = 0, False, False
    for char in text:
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in "[{":
            depth += 1
            if depth > maximum_depth:
                raise SourceContractError("JSON nesting exceeds contract")
        elif char in "]}":
            depth -= 1
            if depth < 0:
                raise SourceContractError("unbalanced JSON")
    try:
        return json.loads(text, object_pairs_hook=_pairs, parse_float=exact_decimal,
                          parse_int=_json_integer, parse_constant=_constant)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise SourceContractError("invalid bounded JSON") from exc


@dataclass(frozen=True)
class ProviderRead:
    scope: SourceScope
    operation: str
    method: str
    path: str
    query: tuple[tuple[str, str], ...] = ()
    body: Mapping[str, Any] | None = None
    contract_id: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.scope, SourceScope) or type(self.query) is not tuple or len(self.query) > 32:
            raise SourceContractError("typed scope and bounded immutable query required")
        if any(type(item) is not tuple or len(item) != 2 or any(not isinstance(value, str) for value in item)
               for item in self.query):
            raise SourceContractError("query entries must be immutable string pairs")
        def freeze(value):
            if isinstance(value, Mapping):
                return MappingProxyType({key: freeze(item) for key, item in value.items()})
            if isinstance(value, (list, tuple)):
                return tuple(freeze(item) for item in value)
            if value is None or isinstance(value, (str, int, bool, Decimal)):
                return value
            raise SourceContractError("unsupported immutable read value")
        if self.body is not None:
            object.__setattr__(self, "body", freeze(self.body))

    @property
    def provider(self) -> str:
        return self.scope.provider

    @property
    def operation_key(self) -> str:
        return canonical_digest({"scope": self.scope, "operation": self.operation, "method": self.method,
                                 "path": self.path, "query": self.query, "body": self.body,
                                 "contract_id": self.contract_id})


@dataclass(frozen=True)
class RawResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes

    def __post_init__(self) -> None:
        if type(self.status) is not int or not 100 <= self.status <= 599 or type(self.body) is not bytes:
            raise SourceContractError("raw HTTP status and byte body required")
        if not isinstance(self.headers, Mapping) or any(not isinstance(key, str) or not isinstance(value, str)
                                                      for key, value in self.headers.items()):
            raise SourceContractError("HTTP headers must be strings")
        object.__setattr__(self, "headers", MappingProxyType(dict(self.headers)))


def validate_read(read: ProviderRead) -> None:
    """Reconstruct each allowed operation; caller-created objects earn no latitude."""
    if not isinstance(read, ProviderRead) or type(read.query) is not tuple:
        raise SourceContractError("typed fixed read required")
    if len(dict(read.query)) != len(read.query):
        raise SourceContractError("duplicate query parameter")
    if read.method not in {"GET", "POST"} or not read.path.startswith("/") or any(token in read.path for token in ("..", ":", "?", "#", "\\", "//")):
        raise SourceContractError("unsafe read route")
    # Imports are lazy to keep provider construction and validation one contract.
    if read.provider == "qbo":
        from .qbo import validate_operation
    elif read.provider == "xero":
        from .xero import validate_operation
    elif read.provider == "sage_intacct":
        from .sage_intacct import validate_operation
    elif read.provider == "netsuite":
        from .netsuite import validate_operation
    else:
        raise SourceContractError("unsupported provider")
    validate_operation(read)


def retry_after_seconds(value: str, *, now: datetime | None = None) -> float:
    if not isinstance(value, str) or len(value) > 128:
        raise SourceContractError("invalid Retry-After")
    if re.fullmatch(r"\d+", value):
        return float(int(value))
    try:
        target = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise SourceContractError("invalid Retry-After") from exc
    if target.tzinfo is None:
        raise SourceContractError("Retry-After requires timezone")
    current = now or datetime.now(timezone.utc)
    return max(0.0, (target - current).total_seconds())


class BoundedTransport:
    def __init__(self, sender: Callable[..., RawResponse], budget: ReadBudget,
                 *, sleep: Callable[[float], None] = time.sleep) -> None:
        self.sender, self.budget, self.sleep = sender, budget, sleep

    def send(self, read: ProviderRead) -> tuple[RawResponse, Any]:
        validate_read(read)
        key = read.operation_key
        while True:
            self.budget.admit_request(key)
            response = self.sender(read, timeout=self.budget.timeout())
            if not isinstance(response, RawResponse):
                raise SourceContractError("sender must return raw response, never decoded floats")
            self.budget.account_response(response.body)
            if response.status == 429 or 500 <= response.status <= 599:
                # Do not wait after the last permitted physical send.
                if self.budget.attempts[key] >= self.budget.limits.attempts_per_operation:
                    raise BudgetExceeded("physical retry count exhausted")
                headers = {str(k).lower(): v for k, v in response.headers.items()}
                delay = retry_after_seconds(headers["retry-after"]) if "retry-after" in headers else 1.0
                self.budget.wait(delay, sleep=self.sleep)
                continue
            if response.status in {301, 302, 303, 307, 308}:
                raise SourceContractError("provider redirects are forbidden")
            if response.status not in {200, 202}:
                raise SourceContractError(f"provider read failed: HTTP {response.status}")
            parsed = strict_json_loads(response.body, maximum_bytes=self.budget.limits.response_bytes,
                                       maximum_depth=self.budget.limits.depth)
            self.budget.remaining()
            return response, parsed

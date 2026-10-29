"""Exact source observations, independent of ORM, provider clients and AI input."""
from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass
from datetime import date, datetime, timezone
from decimal import Context, Decimal, InvalidOperation, localcontext
import hashlib
import json
import re
from typing import Any, Mapping

PROVIDERS = frozenset({"qbo", "xero", "netsuite", "sage_intacct"})
DATASETS = frozenset({"gl", "accounts", "ap", "ar", "credits", "payments", "reports", "budget"})


class SourceContractError(ValueError):
    """Ambiguous or unsupported source data; never a successful empty extract."""


class CapabilityUnavailable(SourceContractError):
    def __init__(self, code: str, dataset: str, detail: str) -> None:
        self.gap = CapabilityGap(code, dataset, detail)
        super().__init__(f"{code}: {detail}")


class BudgetExceeded(SourceContractError):
    """A bounded partition ended incomplete; its deadline is never renewed."""


def exact_decimal(value: Decimal | str | int) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (Decimal, str, int)):
        raise SourceContractError("finite exact decimal required; floats are forbidden")
    if isinstance(value, str) and (len(value) > 128 or not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", value)):
        raise SourceContractError("invalid exact decimal lexeme")
    try:
        number = Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise SourceContractError("invalid decimal") from exc
    if not number.is_finite() or len(number.as_tuple().digits) > 28 or abs(number.adjusted()) > 24:
        raise SourceContractError("nonfinite or out-of-range decimal")
    return number


def required_text(value: Any, name: str, *, maximum: int = 200) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or any(ord(c) < 32 for c in value):
        raise SourceContractError(f"{name}: bounded nonempty string required")
    return value


def strict_date(value: Any, name: str = "date") -> date:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise SourceContractError(f"{name}: ISO calendar date required")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise SourceContractError(f"{name}: invalid calendar date") from exc


def canonical_data(value: Any) -> Any:
    """Normalized exact persisted data. Floats cannot enter a digest by accident."""
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: canonical_data(getattr(value, field.name)) for field in fields(value)}
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        raise SourceContractError("float in canonical source data")
    if isinstance(value, Decimal):
        # Preserve declared decimal scale in observations and avoid exponent forms.
        return format(exact_decimal(value), "f")
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise SourceContractError("timezone required")
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise SourceContractError("canonical object keys must be strings")
        return {key: canonical_data(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [canonical_data(item) for item in value]
    raise SourceContractError("unsupported canonical source value")


def canonical_json(value: Any) -> str:
    return json.dumps(canonical_data(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def canonical_digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SourceScope:
    """Retained server scope. Constructing this data object does not authenticate it."""
    organization_id: int
    company_id: int
    connection_id: int
    book_id: int
    provider: str
    environment: str
    external_account_id: str
    external_entity_id: str
    external_book_id: str
    basis: str
    currency: str
    period_start: date
    period_end: date
    review_period: str = ""

    def __post_init__(self) -> None:
        if any(type(getattr(self, key)) is not int or getattr(self, key) <= 0
               for key in ("organization_id", "company_id", "connection_id", "book_id")):
            raise SourceContractError("canonical positive owner and book ids required")
        if self.provider not in PROVIDERS or self.environment not in {"sandbox", "production"}:
            raise SourceContractError("unknown provider/environment")
        for key in ("external_account_id", "external_entity_id", "external_book_id"):
            required_text(getattr(self, key), key)
        if self.basis not in {"cash", "accrual"} or not re.fullmatch(r"[A-Z]{3}", self.currency):
            raise SourceContractError("explicit supported basis and currency required")
        if type(self.period_start) is not date or type(self.period_end) is not date or self.period_start > self.period_end:
            raise SourceContractError("ordered extraction date window required")
        if self.review_period:
            if not re.fullmatch(r"\d{4}-\d{2}", self.review_period):
                raise SourceContractError("review_period must be YYYY-MM")
            strict_date(f"{self.review_period}-01", "review_period")

    @property
    def organization_company_id(self) -> int:
        return self.company_id

    @property
    def org_id(self) -> int:
        return self.organization_id


@dataclass(frozen=True)
class CapabilityGap:
    code: str
    dataset: str
    detail: str


@dataclass(frozen=True)
class SourceLine:
    source_id: str
    occurrence: str
    account_id: str
    posting_date: date
    debit: Decimal
    credit: Decimal
    currency: str
    source_version: str = "1"
    transaction_id: str = ""
    line_id: str = ""
    source_link: str = ""
    source_currency: str = ""
    source_amount: Decimal | None = None
    rate: Decimal | None = None
    reverses_id: str = ""
    snapshot_local: bool = False

    def __post_init__(self) -> None:
        for key in ("source_id", "occurrence", "account_id", "source_version"):
            required_text(getattr(self, key), key, maximum=500)
        for key in ("transaction_id", "line_id", "source_link", "source_currency", "reverses_id"):
            value = getattr(self, key)
            if not isinstance(value, str) or len(value) > 500 or any(ord(char) < 32 for char in value):
                raise SourceContractError("bounded optional source identity required")
        if type(self.snapshot_local) is not bool:
            raise SourceContractError("explicit snapshot-local identity flag required")
        if type(self.posting_date) is not date or not re.fullmatch(r"[A-Z]{3}", self.currency):
            raise SourceContractError("posting date/currency required")
        for value in (self.debit, self.credit):
            if not isinstance(value, Decimal) or exact_decimal(value) < 0:
                raise SourceContractError("debit/credit must be nonnegative Decimal")
        if self.debit and self.credit:
            raise SourceContractError("one source line cannot be both debit and credit")
        if self.source_amount is not None:
            exact_decimal(self.source_amount)
            if not re.fullmatch(r"[A-Z]{3}", self.source_currency):
                raise SourceContractError("transaction currency required with source amount")
        if self.rate is not None and exact_decimal(self.rate) <= 0:
            raise SourceContractError("positive observed exchange rate required")

    @property
    def net(self) -> Decimal:
        with localcontext(Context(prec=60)):
            return self.debit - self.credit


@dataclass(frozen=True)
class ParsedPage:
    records: tuple[SourceLine, ...]
    next_cursor: str | None
    terminal: bool
    raw_sha256: str
    contract_id: str
    gaps: tuple[CapabilityGap, ...] = ()
    reported_count: int | None = None

    def __post_init__(self) -> None:
        if type(self.records) is not tuple or any(not isinstance(row, SourceLine) for row in self.records):
            raise SourceContractError("typed immutable source records required")
        if type(self.terminal) is not bool or self.terminal != (self.next_cursor is None):
            raise SourceContractError("unambiguous terminal cursor required")
        if self.next_cursor is not None:
            required_text(self.next_cursor, "next_cursor", maximum=200)
        if self.reported_count is not None and (type(self.reported_count) is not int or self.reported_count < 0):
            raise SourceContractError("nonnegative reported count required")
        if not re.fullmatch(r"[0-9a-f]{64}", self.raw_sha256):
            raise SourceContractError("raw source digest required")
        required_text(self.contract_id, "contract_id")


@dataclass(frozen=True)
class SourceObservation:
    """Retained raw response and its exact parser result, before qualification."""
    page: ParsedPage
    raw: bytes
    cursor: str | None
    observed_start: datetime
    observed_end: datetime

    def __post_init__(self) -> None:
        if type(self.raw) is not bytes or hashlib.sha256(self.raw).hexdigest() != self.page.raw_sha256:
            raise SourceContractError("observation raw bytes differ from parsed page")
        if not isinstance(self.observed_start, datetime) or not isinstance(self.observed_end, datetime) or self.observed_start.tzinfo is None or self.observed_end.tzinfo is None or self.observed_start > self.observed_end:
            raise SourceContractError("ordered aware observation interval required")


def adapter_for(provider: str):
    """Closed registry; never import a task-supplied module or callable."""
    if provider == "xero":
        from .xero import XeroAdapter
        return XeroAdapter()
    if provider == "qbo":
        from .qbo import QboAdapter
        return QboAdapter()
    if provider == "netsuite":
        from .netsuite import NetSuiteAdapter
        return NetSuiteAdapter()
    if provider == "sage_intacct":
        from .sage_intacct import SageIntacctAdapter
        return SageIntacctAdapter()
    raise SourceContractError("unknown provider registry key")


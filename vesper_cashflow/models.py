"""Validated canonical inputs; all currency amounts remain exact strings."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from decimal import Decimal, InvalidOperation

from .domain.money import Money

SCHEMA_VERSION = 1
ALGORITHM_VERSION = "sprint-exact-1+vesper-occurrence-1"
COVERAGE_SECTIONS = ("opening_cash", "ar", "ap", "payroll", "rent", "taxes", "financing", "future_sales")
COVERAGE_STATES = {"complete", "partial", "missing", "not_applicable"}


def canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def text(value: object, field: str, limit: int = 255, *, empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > limit or (not empty and not value.strip()):
        raise ValueError(f"{field} must be text of 1–{limit} characters")
    return value.strip()


def iso_date(value: object, field: str, *, optional: bool = False) -> str | None:
    if value in (None, "") and optional:
        return None
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError(f"{field} must be YYYY-MM-DD")
    parsed = date.fromisoformat(value)
    if not 1900 <= parsed.year <= 2200:
        raise ValueError(f"{field} must fall between 1900 and 2200")
    return parsed.isoformat()


def amount(value: object, currency: str, field: str, *, positive: bool = False) -> str:
    if not isinstance(value, str) or len(value) > 40 or not re.fullmatch(r"-?\d+(?:\.\d+)?", value):
        raise ValueError(f"{field} must be an exact decimal string without commas or exponents")
    try:
        money = Money(Decimal(value), currency)
    except (ValueError, TypeError, InvalidOperation) as exc:
        raise ValueError(f"{field}: {exc}") from exc
    if abs(money.amount) > Decimal("999999999999999"):
        raise ValueError(f"{field} is outside the supported amount range")
    if positive and money.amount <= 0:
        raise ValueError(f"{field} must be positive; direction specifies in or out")
    return format(money.amount, "f")


def _closed(value: object, allowed: set[str], name: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    extra = set(value) - allowed
    if extra:
        raise ValueError(f"{name} has unsupported fields: {', '.join(sorted(extra))}")
    return value


def normalize_inputs(raw: dict) -> dict:
    raw = _closed(
        raw,
        {
            "schema_version",
            "as_of",
            "currency",
            "opening_cash",
            "opening_source",
            "items",
            "recurrences",
            "coverage",
            "sources",
            "notes",
        },
        "inputs",
    )
    if raw.get("schema_version", SCHEMA_VERSION) != SCHEMA_VERSION:
        raise ValueError("Unsupported input schema version")
    currency = text(raw.get("currency"), "currency", 3)
    Money.zero(currency)
    as_of = iso_date(raw.get("as_of"), "as_of")
    opening = amount(raw.get("opening_cash"), currency, "opening_cash")
    opening_source = text(raw.get("opening_source"), "opening_source", 500)
    coverage_in = _closed(raw.get("coverage", {}), set(COVERAGE_SECTIONS), "coverage")
    coverage = {key: coverage_in.get(key, "missing") for key in COVERAGE_SECTIONS}
    if any(value not in COVERAGE_STATES for value in coverage.values()):
        raise ValueError("Coverage must be complete, partial, missing, or not_applicable")
    if coverage["opening_cash"] != "complete":
        raise ValueError("A complete dated opening-cash source is required")
    items_raw, recurrence_raw = raw.get("items", []), raw.get("recurrences", [])
    if not isinstance(items_raw, list) or len(items_raw) > 10000:
        raise ValueError("items must be a list of at most 10,000 rows")
    if not isinstance(recurrence_raw, list) or len(recurrence_raw) > 1000:
        raise ValueError("recurrences must be a list of at most 1,000 assumptions")
    items, recurrences, ids = [], [], set()
    for index, entry in enumerate(items_raw):
        row = _closed(
            entry,
            {
                "id",
                "name",
                "category",
                "direction",
                "amount",
                "currency",
                "due_date",
                "expected_date",
                "timing_basis",
                "source",
                "source_id",
                "ordinal",
            },
            f"item {index + 1}",
        )
        item_id = text(row.get("id"), "id", 100)
        if item_id in ids or item_id.startswith("recurrence:"):
            raise ValueError("Input identities must be unique and cannot use the reserved recurrence: prefix")
        ids.add(item_id)
        if row.get("currency", currency) != currency:
            raise ValueError("Mixed-currency aggregation is unsupported; import a separate workspace")
        if row.get("direction") not in {"in", "out"}:
            raise ValueError("Item direction must be in or out")
        category = row.get("category", "ar" if row["direction"] == "in" else "ap")
        if category not in {"ar", "ap", "assumption"}:
            raise ValueError("Item category must be ar, ap, or assumption")
        if (category == "ar" and row["direction"] != "in") or (
            category == "ap" and row["direction"] != "out"
        ):
            raise ValueError("AR items must be cash in and AP items must be cash out")
        expected = iso_date(row.get("expected_date"), "expected_date", optional=True)
        basis = text(row.get("timing_basis", ""), "timing_basis", 500, empty=True)
        if expected and not basis:
            raise ValueError("An expected cash date requires an explicit timing basis")
        ordinal = row.get("ordinal", index + 1)
        if type(ordinal) is not int or not 1 <= ordinal <= 10000:
            raise ValueError("Source row ordinal must be an integer of 1–10,000")
        items.append(
            {
                "id": item_id,
                "name": text(row.get("name"), "name"),
                "category": category,
                "direction": row["direction"],
                "amount": amount(row.get("amount"), currency, "amount", positive=True),
                "currency": currency,
                "due_date": iso_date(row.get("due_date"), "due_date", optional=True),
                "expected_date": expected,
                "timing_basis": basis,
                "source": text(row.get("source"), "source", 500),
                "source_id": text(row.get("source_id", item_id), "source_id", 150),
                "ordinal": ordinal,
            }
        )
    for index, entry in enumerate(recurrence_raw):
        row = _closed(
            entry,
            {
                "id",
                "name",
                "direction",
                "amount",
                "currency",
                "cadence",
                "anchor_date",
                "start_date",
                "end_date",
                "timing_basis",
                "source",
            },
            f"recurrence {index + 1}",
        )
        item_id = text(row.get("id"), "recurrence id", 100)
        if item_id in ids:
            raise ValueError("Input identities must be unique")
        ids.add(item_id)
        if row.get("currency", currency) != currency:
            raise ValueError("Mixed-currency aggregation is unsupported")
        if row.get("direction") not in {"in", "out"} or row.get("cadence") not in {
            "weekly",
            "biweekly",
            "monthly",
        }:
            raise ValueError("Recurrence needs direction in/out and cadence weekly/biweekly/monthly")
        anchor = iso_date(row.get("anchor_date"), "anchor_date")
        start = iso_date(row.get("start_date") or anchor, "start_date")
        end = iso_date(row.get("end_date"), "end_date", optional=True)
        if end and end < start:
            raise ValueError("Recurrence end_date precedes start_date")
        recurrences.append(
            {
                "id": item_id,
                "name": text(row.get("name"), "name"),
                "direction": row["direction"],
                "amount": amount(row.get("amount"), currency, "amount per occurrence", positive=True),
                "currency": currency,
                "cadence": row["cadence"],
                "anchor_date": anchor,
                "start_date": start,
                "end_date": end,
                "timing_basis": text(row.get("timing_basis"), "timing_basis", 500),
                "source": text(row.get("source"), "source", 500),
            }
        )
    sources = raw.get("sources", [])
    if not isinstance(sources, list) or len(sources) > 30:
        raise ValueError("At most 30 source records are supported")
    clean_sources = []
    for entry in sources:
        src = _closed(entry, {"name", "sha256", "observed_at", "kind", "details"}, "source")
        sha = src.get("sha256")
        if not isinstance(sha, str) or not re.fullmatch(r"[a-f0-9]{64}", sha):
            raise ValueError("Every source requires a SHA-256 digest")
        clean_sources.append(
            {
                "name": text(src.get("name"), "source name", 500),
                "sha256": sha,
                "observed_at": text(src.get("observed_at", "not recorded"), "observed_at", 100),
                "kind": text(src.get("kind", "import"), "source kind", 50),
                "details": text(src.get("details", ""), "source details", 2000, empty=True),
            }
        )
    if not clean_sources:
        raise ValueError("At least one source provenance record is required")
    return {
        "schema_version": SCHEMA_VERSION,
        "as_of": as_of,
        "currency": currency,
        "opening_cash": opening,
        "opening_source": opening_source,
        "items": items,
        "recurrences": recurrences,
        "coverage": coverage,
        "sources": clean_sources,
        "notes": text(raw.get("notes", ""), "notes", 2000, empty=True),
    }

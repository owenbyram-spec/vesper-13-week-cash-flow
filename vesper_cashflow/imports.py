"""Bounded CSV acquisition; due dates never become implied cash dates."""

from __future__ import annotations

import csv
import hashlib
import io
from datetime import datetime, timezone

from .models import normalize_inputs, digest

OPENING_COLUMNS = ["as_of", "amount", "currency", "source"]
ITEM_COLUMNS = [
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
]
RECURRING_COLUMNS = [
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
]


def parse_csv(
    contents: str, columns: list[str], label: str, max_rows: int = 10000
) -> tuple[list[dict], dict]:
    if not isinstance(contents, str) or len(contents.encode()) > 2_000_000:
        raise ValueError(f"{label} must be UTF-8 CSV smaller than 2 MB")
    try:
        reader = csv.DictReader(io.StringIO(contents.lstrip("\ufeff")), strict=True)
        if reader.fieldnames != columns:
            raise ValueError(f"{label} header must be: {','.join(columns)}")
        rows = []
        for ordinal, row in enumerate(reader, 1):
            if ordinal > max_rows:
                raise ValueError(f"{label} exceeds {max_rows} rows")
            if None in row or any(value is None for value in row.values()):
                raise ValueError(f"{label} row {ordinal} has the wrong number of columns")
            rows.append({key: value.strip() for key, value in row.items()})
    except csv.Error as exc:
        raise ValueError(f"{label} is malformed CSV: {exc}") from exc
    source = {
        "name": label,
        "sha256": hashlib.sha256(contents.encode()).hexdigest(),
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "kind": "csv",
        "details": f"{len(rows)} rows; no invalid rows dropped",
    }
    return rows, source


def import_csvs(raw: dict) -> dict:
    allowed = {"opening_csv", "items_csv", "recurring_csv", "coverage", "notes"}
    if not isinstance(raw, dict) or set(raw) - allowed:
        raise ValueError("Import accepts opening_csv, items_csv, recurring_csv, coverage and notes only")
    opening, source = parse_csv(raw.get("opening_csv", ""), OPENING_COLUMNS, "Dated opening cash", 1)
    if len(opening) != 1:
        raise ValueError("Opening-cash CSV requires exactly one dated anchor")
    items, item_source = parse_csv(
        raw.get("items_csv", ",".join(ITEM_COLUMNS) + "\n"), ITEM_COLUMNS, "AR/AP and one-time cash items"
    )
    recurrences, recurrence_source = parse_csv(
        raw.get("recurring_csv", ",".join(RECURRING_COLUMNS) + "\n"),
        RECURRING_COLUMNS,
        "Per-occurrence recurring assumptions",
        1000,
    )
    anchor = opening[0]
    return normalize_inputs(
        {
            "as_of": anchor["as_of"],
            "currency": anchor["currency"],
            "opening_cash": anchor["amount"],
            "opening_source": anchor["source"],
            "items": items,
            "recurrences": recurrences,
            "coverage": raw.get("coverage", {"opening_cash": "complete"}),
            "sources": [source, item_source, recurrence_source],
            "notes": raw.get("notes", ""),
        }
    )


def synthetic_demo() -> dict:
    """A complete labelled teaching example, never represented as a client."""
    payload = {
        "as_of": "2026-10-25",
        "currency": "USD",
        "opening_cash": "25000.00",
        "opening_source": "Synthetic reconciled cash at 2026-10-25 close",
        "items": [
            {
                "id": "ar-101",
                "category": "ar",
                "name": "Synthetic client A • October invoice",
                "direction": "in",
                "amount": "12000.00",
                "currency": "USD",
                "due_date": "2026-10-20",
                "expected_date": "2026-10-30",
                "timing_basis": "Fictional collection commitment for this synthetic example",
                "source": "Synthetic AR snapshot",
            },
            {
                "id": "ar-102",
                "category": "ar",
                "name": "Synthetic client B • milestone",
                "direction": "in",
                "amount": "8000.00",
                "currency": "USD",
                "due_date": "2026-11-10",
                "expected_date": "2026-11-13",
                "timing_basis": "Synthetic three-day collection lag",
                "source": "Synthetic AR snapshot",
            },
            {
                "id": "ap-201",
                "category": "ap",
                "name": "Synthetic annual insurance",
                "direction": "out",
                "amount": "6000.00",
                "currency": "USD",
                "due_date": "2026-11-02",
                "expected_date": "2026-11-02",
                "timing_basis": "Synthetic scheduled payment",
                "source": "Synthetic AP snapshot",
            },
            {
                "id": "tax-1",
                "category": "assumption",
                "name": "Synthetic quarterly taxes",
                "direction": "out",
                "amount": "4500.00",
                "currency": "USD",
                "due_date": "2027-01-15",
                "expected_date": "2027-01-15",
                "timing_basis": "Synthetic explicit tax assumption",
                "source": "Synthetic CFO assumptions",
            },
        ],
        "recurrences": [
            {
                "id": "payroll",
                "name": "Payroll",
                "direction": "out",
                "amount": "1000.00",
                "currency": "USD",
                "cadence": "biweekly",
                "anchor_date": "2026-10-30",
                "start_date": "2026-10-30",
                "end_date": "2027-01-24",
                "timing_basis": "$1,000 per payday, every two weeks; synthetic teaching example",
                "source": "Synthetic payroll assumption",
            },
            {
                "id": "rent",
                "name": "Office rent",
                "direction": "out",
                "amount": "2500.00",
                "currency": "USD",
                "cadence": "monthly",
                "anchor_date": "2026-11-01",
                "start_date": "2026-11-01",
                "end_date": "2027-01-24",
                "timing_basis": "Synthetic lease: first day of each month",
                "source": "Synthetic rent assumption",
            },
            {
                "id": "sales",
                "name": "Future service collections",
                "direction": "in",
                "amount": "1500.00",
                "currency": "USD",
                "cadence": "weekly",
                "anchor_date": "2026-11-20",
                "start_date": "2026-11-20",
                "end_date": "2027-01-24",
                "timing_basis": "Synthetic future sales net cash collections, excludes invoices above",
                "source": "Synthetic CFO assumptions",
            },
        ],
        "coverage": {
            "opening_cash": "complete",
            "ar": "complete",
            "ap": "complete",
            "payroll": "complete",
            "rent": "complete",
            "taxes": "complete",
            "financing": "not_applicable",
            "future_sales": "complete",
        },
        "notes": "SYNTHETIC: fictional entity and assumptions. All amounts are examples. Financing is assumed not applicable in this fictional example.",
    }
    payload["sources"] = [
        {
            "name": "Complete synthetic teaching scenario",
            "sha256": digest(payload),
            "observed_at": "2026-10-25T23:59:59Z",
            "kind": "synthetic",
            "details": "Fictional data; no accounting connection or API key required.",
        }
    ]
    return normalize_inputs(payload)

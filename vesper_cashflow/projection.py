"""Bounded adapter: per-occurrence assumptions become dated engine items."""

from __future__ import annotations

import calendar
from datetime import date, timedelta
from decimal import Decimal

from .domain.money import Money
from .domain.forecast import (
    CashFlowDirection,
    CashFlowItem,
    CashFlowForecastInput,
    build_forecast_weeks,
    build_13_week_projection,
    compute_liquidity_summary,
)
from .models import ALGORITHM_VERSION, digest, normalize_inputs


def recurrence_dates(row: dict, first: date, last: date) -> list[date]:
    anchor = date.fromisoformat(row["anchor_date"])
    start = max(first, date.fromisoformat(row["start_date"]))
    end = min(last, date.fromisoformat(row["end_date"])) if row.get("end_date") else last
    if end < start:
        return []
    dates = []
    if row["cadence"] in {"weekly", "biweekly"}:
        step = 7 if row["cadence"] == "weekly" else 14
        offset = max(0, (start - anchor).days)
        occurrence = anchor + timedelta(days=((offset + step - 1) // step) * step)
        while occurrence <= end:
            if occurrence >= start:
                dates.append(occurrence)
            occurrence += timedelta(days=step)
    else:
        year, month = max(anchor, start).year, max(anchor, start).month
        while (year, month) <= (end.year, end.month):
            occurrence = date(year, month, min(anchor.day, calendar.monthrange(year, month)[1]))
            if max(anchor, start) <= occurrence <= end:
                dates.append(occurrence)
            year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return dates


def project(raw: dict) -> dict:
    inputs = normalize_inputs(raw)
    currency = inputs["currency"]
    as_of = date.fromisoformat(inputs["as_of"])
    weeks = build_forecast_weeks(as_of)
    findings, dated, occurrences = [], [], []
    for section, state in inputs["coverage"].items():
        if state in {"missing", "partial"}:
            findings.append(
                {
                    "code": "coverage",
                    "severity": "warning",
                    "ids": [section],
                    "message": f"{section.replace('_', ' ').upper()} coverage is {state}; projected totals exclude unprovided cash flows.",
                }
            )
    for row in inputs["items"]:
        if not row["due_date"]:
            findings.append(
                {
                    "code": "missing_due_date",
                    "severity": "warning",
                    "ids": [row["id"]],
                    "message": f"{row['name']}: source due date is missing.",
                }
            )
        elif row["due_date"] <= inputs["as_of"]:
            findings.append(
                {
                    "code": "overdue_source",
                    "severity": "info",
                    "ids": [row["id"]],
                    "message": f"{row['name']} has an overdue source due date; expected cash timing is a separate assumption.",
                }
            )
        if not row["expected_date"]:
            findings.append(
                {
                    "code": "undated",
                    "severity": "warning",
                    "ids": [row["id"]],
                    "message": f"{row['name']} ({row['amount']} {currency}) is omitted until expected cash timing is reviewed.",
                }
            )
            continue
        expected = date.fromisoformat(row["expected_date"])
        if expected <= as_of:
            findings.append(
                {
                    "code": "past_expected",
                    "severity": "warning",
                    "ids": [row["id"]],
                    "message": f"{row['name']} has expected timing on/before opening cash; omitted to avoid double counting. Review and re-date it.",
                }
            )
            continue
        if expected > weeks[-1].end:
            findings.append(
                {
                    "code": "beyond_horizon",
                    "severity": "info",
                    "ids": [row["id"]],
                    "message": f"{row['name']} ({row['amount']} {currency}) falls after the 13-week horizon and is excluded.",
                }
            )
        dated.append(
            CashFlowItem(
                item_id=row["id"],
                label=row["name"],
                direction=CashFlowDirection.CASH_IN
                if row["direction"] == "in"
                else CashFlowDirection.CASH_OUT,
                expected_date=expected,
                amount=Money(Decimal(row["amount"]), currency),
            )
        )
    for row in inputs["recurrences"]:
        scheduled = recurrence_dates(row, weeks[0].start, weeks[-1].end)
        if not scheduled:
            findings.append(
                {
                    "code": "no_occurrences",
                    "severity": "info",
                    "ids": [row["id"]],
                    "message": f"{row['name']} has no occurrences in this horizon.",
                }
            )
        for expected in scheduled:
            occurrence = {
                "id": f"recurrence:{row['id']}:{expected.isoformat()}",
                "recurrence_id": row["id"],
                "name": row["name"],
                "direction": row["direction"],
                "amount": row["amount"],
                "currency": currency,
                "expected_date": expected.isoformat(),
                "timing_basis": row["timing_basis"],
                "source": row["source"],
            }
            occurrences.append(occurrence)
            dated.append(
                CashFlowItem(
                    item_id=occurrence["id"],
                    label=row["name"],
                    direction=CashFlowDirection.CASH_IN
                    if row["direction"] == "in"
                    else CashFlowDirection.CASH_OUT,
                    expected_date=expected,
                    amount=Money(Decimal(row["amount"]), currency),
                )
            )
    # Deliberately pass NO monthly_average recurrences into the extracted engine.
    projection = build_13_week_projection(
        CashFlowForecastInput(
            as_of=as_of,
            weeks=weeks,
            opening_cash=Money(Decimal(inputs["opening_cash"]), currency),
            items=tuple(dated),
        )
    )
    liquidity = compute_liquidity_summary(projection)
    weekly = [
        {
            "number": index + 1,
            "start": row.week.start.isoformat(),
            "end": row.week.end.isoformat(),
            "beginning_cash": format(row.beginning_cash.amount, "f"),
            "cash_in": format(row.cash_in.amount, "f"),
            "cash_out": format(row.cash_out.amount, "f"),
            "ending_cash": format(row.ending_cash.amount, "f"),
        }
        for index, row in enumerate(projection.weekly)
    ]
    result = {
        "algorithm_version": ALGORITHM_VERSION,
        "as_of": inputs["as_of"],
        "currency": currency,
        "opening_cash": inputs["opening_cash"],
        "weeks": weekly,
        "total_in": format(sum(row.cash_in.amount for row in projection.weekly), "f"),
        "total_out": format(sum(row.cash_out.amount for row in projection.weekly), "f"),
        "ending_cash": format(projection.ending_cash.amount, "f"),
        "minimum_cash": {
            "amount": format(liquidity.minimum_cash_balance.amount, "f"),
            "date": liquidity.minimum_cash_date.isoformat(),
            "basis": liquidity.minimum_cash_observation.basis.value,
        },
        "runway_status": liquidity.runway_status.value,
        "first_breach_date": liquidity.first_breach_date.isoformat() if liquidity.first_breach_date else None,
        "coverage": inputs["coverage"],
        "coverage_complete": not any(
            row["code"] in {"coverage", "undated", "past_expected"} for row in findings
        ),
        "findings": findings,
        "occurrences": occurrences,
        "input_hash": digest(inputs),
        "source_hash": digest(inputs["sources"]),
        "omitted_beyond_cash_in": format(projection.audit.beyond_horizon_cash_in.amount, "f"),
        "omitted_beyond_cash_out": format(projection.audit.beyond_horizon_cash_out.amount, "f"),
    }
    result["result_hash"] = digest(result)
    return result

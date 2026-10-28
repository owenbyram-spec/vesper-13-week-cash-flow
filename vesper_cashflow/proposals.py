"""Only bounded assumption/timing changes; approval is a separate UI action."""

from __future__ import annotations

import copy
from datetime import date, timedelta
from decimal import Decimal

from .models import amount, iso_date, text, normalize_inputs


def apply_changes(base: dict, changes: list[dict]) -> dict:
    if not isinstance(changes, list) or not 1 <= len(changes) <= 100:
        raise ValueError("A proposal needs between 1 and 100 changes")
    updated = copy.deepcopy(base)
    seen = set()
    for change in changes:
        if not isinstance(change, dict):
            raise ValueError("Each change must be an object")
        kind = change.get("kind")
        if kind == "timing":
            if set(change) != {"kind", "id", "expected_date", "timing_basis"}:
                raise ValueError("Timing changes require kind, id, expected_date and timing_basis")
            matches = [row for row in updated["items"] if row["id"] == change["id"]]
            if len(matches) != 1:
                raise ValueError("Timing proposal references an unknown item in the active version")
            matches[0]["expected_date"] = iso_date(change["expected_date"], "expected_date")
            matches[0]["timing_basis"] = text(change["timing_basis"], "timing_basis", 500)
        elif kind == "recurrence":
            if set(change) != {"kind", "id", "amount", "timing_basis"}:
                raise ValueError("Recurrence changes require kind, id, amount and timing_basis")
            matches = [row for row in updated["recurrences"] if row["id"] == change["id"]]
            if len(matches) != 1:
                raise ValueError("Recurrence proposal references an unknown assumption")
            matches[0]["amount"] = amount(
                change["amount"], updated["currency"], "amount per occurrence", positive=True
            )
            matches[0]["timing_basis"] = text(change["timing_basis"], "timing_basis", 500)
        else:
            raise ValueError("Proposals may change item timing or a recurring amount only")
        key = (kind, change["id"])
        if key in seen:
            raise ValueError("Duplicate changes for the same assumption are refused")
        seen.add(key)
    return normalize_inputs(updated)


def delay_receipts(base: dict, days: int) -> list[dict]:
    if type(days) is not int or not 1 <= days <= 91:
        raise ValueError("Receipt delay must be an integer of 1–91 days")
    return [
        {
            "kind": "timing",
            "id": row["id"],
            "expected_date": (date.fromisoformat(row["expected_date"]) + timedelta(days=days)).isoformat(),
            "timing_basis": f"Scenario: delay reviewed collection timing by {days} days; source due date unchanged",
        }
        for row in base["items"]
        if row["direction"] == "in" and row["expected_date"]
    ]


def comparison(before: dict, after: dict) -> dict:
    if before["as_of"] != after["as_of"] or before["currency"] != after["currency"]:
        raise ValueError("Compare requires the same as-of date and currency")
    return {
        "ending_cash_delta": format(Decimal(after["ending_cash"]) - Decimal(before["ending_cash"]), "f"),
        "minimum_cash_delta": format(
            Decimal(after["minimum_cash"]["amount"]) - Decimal(before["minimum_cash"]["amount"]), "f"
        ),
        "before_minimum": before["minimum_cash"],
        "after_minimum": after["minimum_cash"],
        "weekly": [
            {
                "number": left["number"],
                "end": left["end"],
                "before": left["ending_cash"],
                "after": right["ending_cash"],
                "delta": format(Decimal(right["ending_cash"]) - Decimal(left["ending_cash"]), "f"),
            }
            for left, right in zip(before["weeks"], after["weeks"])
        ],
    }

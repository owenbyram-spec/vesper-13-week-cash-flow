"""Versioned exact executable JSON identity; never an authorization source."""
from __future__ import annotations

import hashlib
import json
import math
from datetime import timezone

CANONICAL_VERSION = 1
AUTHORITY_KEYS = frozenset({"_current_user_id", "_current_user_email", "_organization_id", "_organization_company_id", "_approved_by"})


def canonical_json(value):
    def validate(item):
        if item is None or type(item) in (str, bool, int):
            return
        if type(item) is float and math.isfinite(item):
            return
        if type(item) is list:
            for child in item:
                validate(child)
            return
        if type(item) is dict and all(type(key) is str for key in item):
            for child in item.values():
                validate(child)
            return
        raise ValueError("Executable intent must contain finite JSON values with string keys")
    validate(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def executable_inputs(action_type, payload):
    if type(payload) is not dict:
        raise ValueError("Executable intent requires a stored object payload")
    if type(payload.get("tool_input")) is dict:
        inputs = {k: v for k, v in payload["tool_input"].items()
                  if k not in AUTHORITY_KEYS | {"_reviewed_budget_plan", "budget_calculation_plan"}}
    elif action_type == "MEMORY_UPDATE":
        inputs = {"fact": payload.get("content"), "category": payload.get("category"),
                  "source": payload.get("source"), "source_metadata": payload.get("source_metadata")}
    else:
        raise ValueError("Executable intent requires stored tool_input")
    if action_type == "BUDGET_APPLY_DRIVERS":
        inputs["budget_calculation_plan"] = payload.get("budget_calculation_plan")
    canonical_json(inputs)
    return inputs


def intent_fingerprint(company_id, action_type, tool_name, inputs):
    value = {"canonical_version": CANONICAL_VERSION, "company_id": company_id,
             "action_type": action_type, "tool_name": tool_name, "inputs": inputs}
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def action_intent_fingerprint(action, tool_name):
    return intent_fingerprint(action.company_id, action.action_type, tool_name,
                              executable_inputs(action.action_type, action.payload))


def approval_fingerprint(action, intent):
    stamp = action.reviewed_at
    if stamp is not None:
        # SQLite drops tzinfo; UTC is the persisted review timestamp contract.
        stamp = stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp.astimezone(timezone.utc)
        stamp = stamp.isoformat()
    return hashlib.sha256(canonical_json({
        "binding_version": 1, "action_id": action.id, "company_id": action.company_id,
        "action_type": action.action_type, "intent": intent, "revision": action.revision,
        "provenance_version": action.provenance_version,
        "human_preparer_user_id": action.human_preparer_user_id,
        "last_human_editor_user_id": action.last_human_editor_user_id,
        "reviewed_by": action.reviewed_by, "reviewed_at": stamp,
    }).encode("utf-8")).hexdigest()

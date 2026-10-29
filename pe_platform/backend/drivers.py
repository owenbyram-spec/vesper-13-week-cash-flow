"""Read-only names observed in a scoped General Ledger reporting window.

The GL supplies the population. Contact lists only establish its identity/type;
they never add names or turn historical activity into future cash flow.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import math
import re
import time
import unicodedata

from fastapi import HTTPException, Query, Request

from .connections import RecordedClient, client_for_scope

LOOKBACK_DAYS = 180
CONTACT_PAGE_SIZE = 500
MAX_CONTACT_PAGES = 100
MAX_GL_ROWS = 100_000
MAX_REPORT_NODES = 200_000
MAX_REPORT_DEPTH = 24
MAX_REPORT_BYTES = 20_000_000
MAX_JSON_NODES = 1_000_000
OPERATION_SECONDS = 120

_MESSAGES = {
    "gl_unavailable": "The General Ledger could not be read for this workspace.",
    "gl_header_invalid": "The General Ledger did not establish the requested report, dates and currency.",
    "gl_columns_invalid": "The General Ledger did not establish unique date and name columns.",
    "gl_rows_invalid": "Some General Ledger rows could not be read reliably.",
    "gl_window_mismatch": "Some General Ledger rows fall outside the requested history window.",
    "gl_truncated": "The General Ledger reports incomplete or truncated coverage.",
    "gl_limit": "The General Ledger exceeds this local directory's read limit.",
    "gl_no_data_conflict": "The General Ledger's no-data indicator conflicts with its rows.",
    "contacts_unavailable": "Customer/vendor identity metadata is incomplete; unresolved names require review.",
    "contact_identity_unresolved": "Some GL names do not establish a unique customer or vendor identity.",
    "operation_deadline": "The GL directory read exceeded its operation deadline.",
}


class CatalogReadError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(_MESSAGES[code])


def _issues(codes):
    return [{"code": code, "message": _MESSAGES[code]} for code in dict.fromkeys(codes)]


def _check_deadline(deadline):
    if time.monotonic() > deadline:
        raise CatalogReadError("operation_deadline")


def _report_size_guard(report, deadline):
    """Bound the decoded JSON before the existing source recorder writes it."""
    stack = [(report, 0)]
    nodes = size = 0
    while stack:
        _check_deadline(deadline)
        value, depth = stack.pop()
        nodes += 1
        if nodes > MAX_JSON_NODES or depth > MAX_REPORT_DEPTH * 4 + 20:
            raise CatalogReadError("gl_limit")
        if isinstance(value, dict):
            if not all(isinstance(key, str) for key in value):
                raise CatalogReadError("gl_rows_invalid")
            if len(value) * 2 > MAX_JSON_NODES - nodes:
                raise CatalogReadError("gl_limit")
            size += 2 + max(0, len(value) - 1) + len(value)
            stack.extend((member, depth + 1) for member in value.values())
            stack.extend((key, depth + 1) for key in value)
        elif isinstance(value, list):
            if len(value) > MAX_JSON_NODES - nodes:
                raise CatalogReadError("gl_limit")
            size += 2 + max(0, len(value) - 1)
            stack.extend((member, depth + 1) for member in value)
        elif isinstance(value, str):
            if size + len(value) > MAX_REPORT_BYTES:
                raise CatalogReadError("gl_limit")
            size += len(json.dumps(value))
        elif value is None or isinstance(value, (bool, int)):
            try:
                size += len(json.dumps(value))
            except ValueError:
                raise CatalogReadError("gl_rows_invalid") from None
        elif isinstance(value, float) and math.isfinite(value):
            size += len(json.dumps(value))
        else:
            raise CatalogReadError("gl_rows_invalid")
        if size > MAX_REPORT_BYTES:
            raise CatalogReadError("gl_limit")


def _text(value):
    if not isinstance(value, str) or len(value) > 1000:
        return None
    return unicodedata.normalize("NFC", value).strip()


def _identity(value):
    if value in (None, ""):
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise CatalogReadError("gl_rows_invalid")
    result = str(value).strip()
    if not result or len(result) > 120:
        raise CatalogReadError("gl_rows_invalid")
    return result


def _column_kind(column):
    if not isinstance(column, dict):
        return None
    metadata = column.get("MetaData", [])
    if not isinstance(metadata, list):
        return None
    keys = [item.get("Value") for item in metadata if isinstance(item, dict) and item.get("Name") == "ColKey"]
    if len(keys) > 1:
        raise CatalogReadError("gl_columns_invalid")
    key = str(keys[0]).strip().lower() if len(keys) == 1 else ""
    kind = str(column.get("ColType", "")).strip().lower()
    title = str(column.get("ColTitle", "")).strip().lower()
    mapping = {"tx_date": "date", "date": "date", "name": "name", "cust_name": "customer", "vend_name": "vendor"}
    if key:
        return mapping.get(key)
    if kind in {"tx_date", "date", "cust_name", "vend_name"}:
        return mapping[kind]
    if kind in {"", "name", "string"}:
        return {"date": "date", "name": "name", "customer": "customer", "vendor": "vendor"}.get(title)
    return None


def _truncated(value):
    """Known explicit limit flags cannot be promoted to complete coverage."""
    if not isinstance(value, dict):
        return False
    flags = {"truncated", "istruncated", "hasmore", "hasmoredata", "partial", "ispartial"}
    for key, member in value.items():
        if str(key).replace("_", "").lower() in flags and str(member).lower() in {"true", "1"}:
            return True
    for item in value.get("Option", []) if isinstance(value.get("Option", []), list) else []:
        if isinstance(item, dict) and str(item.get("Name", "")).replace("_", "").lower() in flags:
            if str(item.get("Value", "")).lower() in {"true", "1"}:
                return True
    return False


def _gl_observations(report, start, end, currency, deadline):
    if not isinstance(report, dict) or report.get("Fault"):
        raise CatalogReadError("gl_header_invalid")
    header = report.get("Header")
    if not isinstance(header, dict) or any(
        header.get(key) != value for key, value in {
            "ReportName": "GeneralLedger", "StartPeriod": start.isoformat(),
            "EndPeriod": end.isoformat(), "Currency": currency,
        }.items()
    ):
        raise CatalogReadError("gl_header_invalid")
    if header.get("ReportBasis") != "Accrual":
        raise CatalogReadError("gl_header_invalid")
    columns = (report.get("Columns") or {}).get("Column")
    if not isinstance(columns, list) or not columns or len(columns) > 100:
        raise CatalogReadError("gl_columns_invalid")
    indices = defaultdict(list)
    for index, column in enumerate(columns):
        kind = _column_kind(column)
        if kind:
            indices[kind].append(index)
    if len(indices["date"]) != 1 or not any(indices[kind] for kind in ("name", "customer", "vendor")):
        raise CatalogReadError("gl_columns_invalid")
    if any(len(values) > 1 for values in indices.values()):
        raise CatalogReadError("gl_columns_invalid")
    if "Option" in header and (
        not isinstance(header["Option"], list) or not all(isinstance(item, dict) for item in header["Option"])
    ):
        raise CatalogReadError("gl_header_invalid")
    no_data_values = [
        str(item.get("Value", "")).lower()
        for item in header.get("Option", []) if isinstance(item, dict) and item.get("Name") == "NoReportData"
    ] if isinstance(header.get("Option", []), list) else []
    if len(no_data_values) > 1 or any(value not in {"true", "false"} for value in no_data_values):
        raise CatalogReadError("gl_header_invalid")
    no_data = no_data_values[0] if no_data_values else None
    container = report.get("Rows")
    if not isinstance(container, dict) or "Row" not in container:
        if no_data == "true" and container in (None, {}):
            rows = []
        else:
            raise CatalogReadError("gl_rows_invalid")
    else:
        rows = container["Row"]
    if not isinstance(rows, list):
        raise CatalogReadError("gl_rows_invalid")
    codes = ["gl_truncated"] if _truncated(report) or _truncated(header) or _truncated(container) else []
    observations = []
    counts = {"data_rows": 0, "named_rows": 0, "unattributed_rows": 0}
    stack = [(item, 0) for item in reversed(rows)]
    visited = 0
    while stack:
        _check_deadline(deadline)
        item, depth = stack.pop()
        visited += 1
        if visited > MAX_REPORT_NODES or depth > MAX_REPORT_DEPTH:
            codes.append("gl_limit")
            break
        if not isinstance(item, dict):
            codes.append("gl_rows_invalid")
            continue
        if _truncated(item):
            codes.append("gl_truncated")
        if item.get("type") == "Section":
            children = item.get("Rows", {})
            if _truncated(children):
                codes.append("gl_truncated")
            if not isinstance(children, dict) or not isinstance(children.get("Row", []), list):
                codes.append("gl_rows_invalid")
            else:
                stack.extend((child, depth + 1) for child in reversed(children.get("Row", [])))
            continue
        if item.get("type") in {"Header", "Summary"}:
            continue
        if item.get("type") != "Data":
            codes.append("gl_rows_invalid")
            continue
        if "Rows" in item:
            codes.append("gl_rows_invalid")
            continue
        cells = item.get("ColData")
        if not isinstance(cells, list):
            codes.append("gl_rows_invalid")
            continue
        date_index = indices["date"][0]
        raw_date = cells[date_index].get("value") if len(cells) > date_index and isinstance(cells[date_index], dict) else None
        if isinstance(raw_date, str) and raw_date.strip().lower() in {"beginning balance", "opening balance", "balance forward"}:
            continue
        counts["data_rows"] += 1
        if counts["data_rows"] > MAX_GL_ROWS:
            counts["data_rows"] -= 1
            codes.append("gl_limit")
            break
        if len(cells) != len(columns) or not all(isinstance(cell, dict) for cell in cells):
            codes.append("gl_rows_invalid")
            continue
        try:
            if not isinstance(raw_date, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw_date):
                raise ValueError()
            observed_date = date.fromisoformat(raw_date)
        except ValueError:
            codes.append("gl_rows_invalid")
            continue
        if not start <= observed_date <= end:
            codes.append("gl_window_mismatch")
            continue
        names = []
        for kind in ("customer", "vendor", "name"):
            if not indices[kind]:
                continue
            cell = cells[indices[kind][0]]
            name = _text(cell.get("value", ""))
            if name is None:
                codes.append("gl_rows_invalid")
                continue
            if not name:
                continue
            try:
                contact_id = _identity(cell.get("id"))
                invalid_id = False
            except CatalogReadError:
                codes.append("gl_rows_invalid")
                contact_id = None
                invalid_id = True
            # A generic Name repeating an explicit party is not another GL party.
            if kind == "name" and any(name == value["name"] and (not contact_id or contact_id == value["id"]) for value in names):
                continue
            names.append({"name": name, "id": contact_id, "invalid_id": invalid_id,
                          "type": kind, "date": observed_date, "line": visited})
        if names:
            counts["named_rows"] += 1
            observations.extend(names)
        else:
            counts["unattributed_rows"] += 1
    if no_data == "true" and counts["data_rows"]:
        raise CatalogReadError("gl_no_data_conflict")
    if no_data == "false" and not rows:
        codes.append("gl_no_data_conflict")
    return observations, counts, codes


def _contacts(client, entity, deadline):
    result, seen, complete = [], set(), True
    try:
        for page in range(MAX_CONTACT_PAGES):
            _check_deadline(deadline)
            position = page * CONTACT_PAGE_SIZE + 1
            response = client.query(
                f"SELECT * FROM {entity} WHERE Active IN (true, false) "
                f"ORDERBY Id STARTPOSITION {position} MAXRESULTS {CONTACT_PAGE_SIZE}"
            )
            query = response.get("QueryResponse") if isinstance(response, dict) else None
            if not isinstance(query, dict) or response.get("Fault") or _truncated(response) or _truncated(query):
                raise ValueError()
            batch = query.get(entity, [])
            if not isinstance(batch, list) or len(batch) > CONTACT_PAGE_SIZE:
                raise ValueError()
            for field, expected in (("startPosition", position), ("maxResults", len(batch))):
                if field in query and (
                    isinstance(query[field], bool) or not isinstance(query[field], int) or query[field] != expected
                ):
                    raise ValueError()
            for row in batch:
                if not isinstance(row, dict):
                    raise ValueError()
                identity = _identity(row.get("Id"))
                name = _text(row.get("DisplayName"))
                if identity is None or not name or identity in seen or (
                    row.get("Active") is not None and not isinstance(row["Active"], bool)
                ):
                    raise ValueError()
                seen.add(identity)
                result.append({"id": identity, "name": name, "active": row.get("Active"), "type": entity.lower()})
            if len(batch) < CONTACT_PAGE_SIZE:
                if "totalCount" in query and (
                    isinstance(query["totalCount"], bool) or not isinstance(query["totalCount"], int)
                    or query["totalCount"] != len(result)
                ):
                    raise ValueError()
                break
        else:
            complete = False
    except HTTPException as error:
        if error.status_code in {403, 409}:
            raise HTTPException(error.status_code, "The displayed workspace or accounting authorization changed; reload before continuing.") from None
        complete = False
    except Exception:
        complete = False
    return result, complete


def _resolve(observation, contacts, complete):
    kind, identity = observation["type"], observation["id"]
    if observation.get("invalid_id"):
        return "unresolved", None, None, "unresolved"
    if kind in {"customer", "vendor"}:
        matches = [item for item in contacts if item["type"] == kind and identity is not None and item["id"] == identity]
        if not complete:
            return kind, identity, None, "typed_gl" if identity is not None else "unresolved"
        if identity is not None:
            if len(matches) == 1:
                return kind, identity, matches[0]["active"], "typed_gl"
            return kind, identity, None, "unresolved"
        named = [item for item in contacts if item["type"] == kind and item["name"] == observation["name"]]
        return kind, None, named[0]["active"] if len(named) == 1 else None, "unique_name" if len(named) == 1 else "unresolved"
    if not complete:
        return "unresolved", identity, None, "unresolved"
    if identity is not None:
        matches = [item for item in contacts if item["id"] == identity]
        if len(matches) == 1:
            return matches[0]["type"], identity, matches[0]["active"], "gl_id"
        return "unresolved", identity, None, "unresolved"
    matches = [item for item in contacts if item["name"] == observation["name"]]
    if len(matches) == 1:
        return matches[0]["type"], None, matches[0]["active"], "unique_name"
    return "unresolved", None, None, "unresolved"


def _entries(observations, contacts, complete, company_id):
    grouped = {}
    rank = {"unresolved": 0, "unique_name": 1, "typed_gl": 2, "gl_id": 3}
    for observation in observations:
        kind, identity, active, basis = _resolve(observation, contacts, complete)
        key = (kind, identity) if kind != "unresolved" and identity is not None else (kind, identity, observation["name"])
        group = grouped.setdefault(key, {"names": set(), "lines": set(), "dates": [], "label": None,
                                        "entity_type": kind, "entity_id": identity, "active": active,
                                        "identity_basis": basis})
        group["names"].add(observation["name"])
        group["lines"].add(observation["line"])
        group["dates"].append(observation["date"])
        label = (observation["date"], observation["name"])
        if group["label"] is None or label > group["label"]:
            group["label"] = label
        if rank[basis] < rank[group["identity_basis"]]:
            group["identity_basis"] = basis
    result = []
    for key, group in grouped.items():
        name = group["label"][1]
        result.append({
            "key": hashlib.sha256(repr((company_id, key)).encode()).hexdigest(),
            "name": name, "entity_type": group["entity_type"], "entity_id": group["entity_id"],
            "aliases": sorted(group["names"] - {name}), "active": group["active"],
            "first_seen": min(group["dates"]).isoformat(), "last_seen": max(group["dates"]).isoformat(),
            "gl_line_count": len(group["lines"]), "identity_basis": group["identity_basis"],
        })
    return sorted(result, key=lambda item: (item["entity_type"], item["name"].casefold(), item["key"]))


def fetch_catalog(client, *, company_id, currency="USD", today=None):
    end = today or date.today()
    start = end - timedelta(days=LOOKBACK_DAYS)
    result = {
        "schema_version": 1, "source": "GeneralLedger", "company_id": company_id,
        "start_date": start.isoformat(), "end_date": end.isoformat(),
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "coverage": {"status": "unavailable", "issues": [], "data_rows": 0, "named_rows": 0, "unattributed_rows": 0},
        "entries": [],
    }
    deadline = time.monotonic() + OPERATION_SECONDS
    try:
        _check_deadline(deadline)
        report = client.get_report("GeneralLedger", {
            "start_date": start.isoformat(), "end_date": end.isoformat(), "accounting_method": "Accrual",
        })
        observations, counts, codes = _gl_observations(report, start, end, currency, deadline)
        result["coverage"].update(counts)
        contacts, complete = [], True
        if observations:
            for entity in ("Customer", "Vendor"):
                rows, current_complete = _contacts(client, entity, deadline)
                contacts.extend(rows)
                complete = complete and current_complete
            if not complete:
                codes.append("contacts_unavailable")
        _check_deadline(deadline)
        result["entries"] = _entries(observations, contacts, complete, company_id)
        if any(item["identity_basis"] in {"unresolved", "unique_name"} for item in result["entries"]):
            codes.append("contact_identity_unresolved")
        result["coverage"].update({"status": "partial" if codes else "complete", "issues": _issues(codes)})
    except HTTPException as error:
        if error.status_code in {403, 409}:
            raise HTTPException(error.status_code, "The displayed workspace or accounting authorization changed; reload before continuing.") from None
        result["coverage"]["issues"] = _issues(["gl_unavailable"])
    except CatalogReadError as error:
        result["coverage"]["issues"] = _issues([error.code])
    except Exception:
        result["coverage"]["issues"] = _issues(["gl_unavailable"])
    result["observed_at"] = datetime.now(timezone.utc).isoformat()
    return result


class CatalogLimitClient:
    """Per-directory guard; all existing authorization/money checks stay inside."""

    def __init__(self, inner, currency):
        self.inner, self.currency = inner, currency
        self.deadline = time.monotonic() + OPERATION_SECONDS

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def get_report(self, name, params=None):
        value = self.inner.get_report(name, params)
        if name == "GeneralLedger":
            _report_size_guard(value, self.deadline)
            start, end = date.fromisoformat(params["start_date"]), date.fromisoformat(params["end_date"])
            _, _, codes = _gl_observations(value, start, end, self.currency, self.deadline)
            if codes:
                raise CatalogReadError(codes[0])
        return value


def register(app, runtime):
    @app.get("/cashflow-forecast/driver-catalog")
    def driver_catalog(request: Request, company_id: str = Query(...)):
        with runtime.financial_lock:
            scope = runtime.require_scope(request)
            if company_id != scope["realm_id"]:
                raise HTTPException(403, "Request company differs from the displayed workspace.")
            runtime.workspaces.require_current(scope)
            client = client_for_scope(runtime, scope, allow_driver_catalog=True)
            if isinstance(client, RecordedClient) and not client.replay and not isinstance(client.client, CatalogLimitClient):
                client.client = CatalogLimitClient(client.client, scope["currency"])
            catalog = fetch_catalog(client, company_id=company_id, currency=scope["currency"])
            runtime.workspaces.require_current(scope)
            return catalog

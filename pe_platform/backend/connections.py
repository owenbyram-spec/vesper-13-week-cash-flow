"""Scoped credentials and a read-only local QBO seam for native acquisition."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import sys
import time
from contextlib import contextmanager
from contextvars import ContextVar
from decimal import Decimal

from fastapi import HTTPException, Request

from vesper_cashflow.connectors.qbo import HOSTS, QBOClient, QBOError
from vesper_cashflow.settings import SettingsError

from .security import validate_amount

_replay_workspace = ContextVar("vesper_pe_replay_workspace", default=None)
_READ_ENTITIES = {
    "Account",
    "Invoice",
    "Bill",
    "Payment",
    "Purchase",
    "BillPayment",
    "Deposit",
    "Transfer",
    "JournalEntry",
    "SalesReceipt",
    "Customer",
    "Vendor",
}
# Default native cash/AR/AP dependencies; no caller-defined report paths.
_READ_REPORTS = {
    "BalanceSheet",
    "AgedReceivables",
    "AgedReceivableDetail",
    "AgedPayables",
    "AgedPayableDetail",
    "ProfitAndLoss",
}


def _plain(value):
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, dict):
        return {key: _plain(member) for key, member in value.items()}
    if isinstance(value, list):
        return [_plain(member) for member in value]
    return value


def _read_key(kind, arguments):
    return hashlib.sha256(
        json.dumps([kind, arguments], sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _validate_currency_money(value, currency):
    if isinstance(value, dict):
        if (
            isinstance(value.get("CurrencyRef"), dict)
            and value["CurrencyRef"].get("value", currency) != currency
        ):
            raise QBOError(
                "The accounting source currency differs from the USD workspace; no FX aggregation is supported."
            )
        for key, member in value.items():
            if key in {"Balance", "TotalAmt", "Amount", "CurrentBalance"} and member is not None:
                validate_amount(member)
            else:
                _validate_currency_money(member, currency)
    elif isinstance(value, list):
        for member in value:
            _validate_currency_money(member, currency)


class NativeReadOnlyClient:
    """Native QBO method names; only fixed GET reads and token refresh can reach HTTP."""

    def __init__(self, runtime, scope, *, allow_driver_catalog=False):
        self.runtime, self.scope = runtime, dict(scope)
        self.realm_id = scope["realm_id"]
        self.allowed_reports = frozenset(_READ_REPORTS) | (
            {"GeneralLedger"} if allow_driver_catalog is True else set()
        )
        self.deadline, self.rows_seen = time.monotonic() + 120, 0
        self.legacy = QBOClient(runtime.settings, scope["id"], lock=runtime.financial_lock)

    def _request(self, endpoint, params):
        if endpoint != "query" and endpoint not in {"reports/" + name for name in self.allowed_reports}:
            raise QBOError("Only cash-flow accounting reads are available.")
        self.runtime.workspaces.require_current(self.scope)
        self.legacy._require_bound()
        with self.runtime.financial_lock:
            bundle = self.legacy._refresh()
            if bundle["realm_id"] != self.scope["realm_id"]:
                raise QBOError("The saved connection is not bound to the displayed entity.")
            url = HOSTS[bundle["environment"]] + "/v3/company/" + bundle["realm_id"] + "/" + endpoint
            for attempt in range(2):
                response = self.legacy._http(
                    "GET",
                    url,
                    params={**params, "minorversion": "73"},
                    headers={
                        "Authorization": "Bearer " + bundle["access_token"],
                        "Accept": "application/json",
                    },
                )
                if response.status_code == 401 and attempt == 0:
                    bundle = self.legacy._refresh(force=True)
                    continue
                result = self.legacy._decode(response)
                self.runtime.workspaces.require_current(self.scope)
                _validate_currency_money(result, self.scope["currency"])
                return _plain(result)
        raise QBOError("Accounting access requires renewed authorisation.")

    def query(self, query):
        match = (
            re.match(r"^\s*SELECT\s+(?:\*|COUNT\(\*\)|[A-Za-z0-9_,\s]+)\s+FROM\s+([A-Za-z]+)\b", query, re.I)
            if isinstance(query, str)
            else None
        )
        if not match or match[1] not in _READ_ENTITIES or len(query) > 10000 or ";" in query:
            raise QBOError("Only bounded accounting SELECT queries are available.")
        if time.monotonic() > self.deadline:
            raise QBOError(
                "The accounting read exceeded its operation deadline; prior financial work is retained."
            )
        result = self._request("query", {"query": query})
        rows = (result.get("QueryResponse") or {}).get(match[1]) or []
        self.rows_seen += len(rows)
        if self.rows_seen > 100000:
            raise QBOError("Accounting acquisition exceeds this local operation's row budget.")
        return result

    def get_report(self, name, params=None):
        if name not in self.allowed_reports:
            raise QBOError("This workspace exposes only the fixed cash-flow and AR/AP summary reports.")
        result = self._request("reports/" + name, params or {})
        if result.get("Header", {}).get("Currency") != self.scope["currency"]:
            raise QBOError("The accounting report must establish the workspace's USD currency.")
        columns = result.get("Columns", {}).get("Column", [])
        indices = [
            index for index, column in enumerate(columns) if str(column.get("ColType", "")).lower() == "money"
        ]

        def walk(value):
            if isinstance(value, dict):
                cells = value.get("ColData")
                if isinstance(cells, list):
                    for index in indices:
                        if index < len(cells) and cells[index].get("value") not in (None, ""):
                            raw = str(cells[index]["value"]).replace(",", "").replace("$", "")
                            if raw.startswith("(") and raw.endswith(")"):
                                raw = "-" + raw[1:-1]
                            validate_amount(raw)
                for member in value.values():
                    walk(member)
            elif isinstance(value, list):
                for member in value:
                    walk(member)

        walk(result.get("Rows", {}))
        return result

    def get_bank_accounts(self):
        return (
            self.query("SELECT * FROM Account WHERE AccountType = 'Bank' MAXRESULTS 1000")
            .get("QueryResponse", {})
            .get("Account", [])
        )

    def get_company_info(self):
        result = self.legacy.company()
        return {
            "CompanyInfo": {"Id": result["id"], "CompanyName": result["name"], "LegalName": result["name"]}
        }

    def get_transaction(self, entity, txn_id):
        if entity not in _READ_ENTITIES or not re.fullmatch(r"[A-Za-z0-9-]{1,80}", str(txn_id)):
            raise QBOError("Unsupported accounting read identity.")
        rows = (
            self.query(f"SELECT * FROM {entity} WHERE Id = '{txn_id}'")
            .get("QueryResponse", {})
            .get(entity, [])
        )
        if not rows:
            raise QBOError("Accounting transaction not found.")
        return {entity: rows[0]}


class RecordedClient:
    def __init__(self, runtime, scope, client=None, *, replay=False, allow_driver_catalog=False):
        self.runtime, self.scope, self.client, self.replay = runtime, scope, client, replay
        self.realm_id = scope["realm_id"]
        self.allow_driver_catalog = allow_driver_catalog is True

    def _read(self, kind, arguments):
        key = _read_key(kind, arguments)
        record = self.runtime.workspaces.read_source(self.scope)
        if self.replay:
            if key not in record.get("reads", {}):
                raise QBOError(
                    "Captured accounting evidence is missing for this preview; refresh and review the source first."
                )
            return copy.deepcopy(record["reads"][key]["value"])
        value = getattr(self.client, kind)(*arguments)
        record.setdefault("reads", {})[key] = {"kind": kind, "arguments": arguments, "value": _plain(value)}
        self.runtime.workspaces.write_source(self.scope, record)
        return value

    def query(self, query):
        return self._read("query", [query])

    def get_report(self, name, params=None):
        if name == "GeneralLedger" and not self.allow_driver_catalog:
            raise QBOError("This workspace exposes only the fixed cash-flow and AR/AP summary reports.")
        return self._read("get_report", [name, params or {}])

    def get_company_info(self):
        return self._read("get_company_info", [])

    def get_bank_accounts(self):
        return self._read("get_bank_accounts", [])

    def get_transaction(self, entity, txn_id):
        return self._read("get_transaction", [entity, txn_id])


@contextmanager
def replay_source(scope):
    token = _replay_workspace.set(scope["id"])
    try:
        yield
    finally:
        _replay_workspace.reset(token)


def is_replaying(scope):
    return _replay_workspace.get() == scope["id"]


def client_for_scope(runtime, scope, *, replay=False, allow_driver_catalog=False):
    if replay or _replay_workspace.get() == scope["id"]:
        return RecordedClient(runtime, scope, replay=True, allow_driver_catalog=allow_driver_catalog)
    if scope["synthetic"]:
        from .fixtures import SyntheticQBOClient, dataset

        source = runtime.workspaces.read_source(scope)
        if not source.get("fixture"):
            source["fixture"] = dataset()
            runtime.workspaces.write_source(scope, source)
        return RecordedClient(
            runtime, scope, SyntheticQBOClient(scope, source["fixture"]),
            allow_driver_catalog=allow_driver_catalog,
        )
    bundle = runtime.settings.get(scope["id"], "qbo")
    if not bundle or not bundle.get("bound_company") or bundle["realm_id"] != scope["realm_id"]:
        raise HTTPException(409, "Save, test and explicitly confirm the QBO company in this workspace first.")
    return RecordedClient(
        runtime, scope, NativeReadOnlyClient(runtime, scope, allow_driver_catalog=allow_driver_catalog),
        allow_driver_catalog=allow_driver_catalog,
    )


def install_native_read_seam(runtime):
    import app.qbo_client as native

    original = native.get_qbo_client_from_db

    def local_client(db, company_id):
        scope = runtime.workspaces.active()
        if not scope or str(company_id) != scope["realm_id"]:
            raise HTTPException(403, "Accounting reads are bound to the server's active workspace.")
        return client_for_scope(runtime, scope)

    native.get_qbo_client_from_db = local_client
    for name, module in tuple(sys.modules.items()):
        if name.startswith("app.") and getattr(module, "get_qbo_client_from_db", None) is original:
            module.get_qbo_client_from_db = local_client


def register(app, runtime):
    @app.get("/local/connections")
    def status(request: Request):
        scope = runtime.require_scope(request)
        model = runtime.settings.get(scope["id"], "model")
        qbo = runtime.settings.get(scope["id"], "qbo")
        # Match the existing settings service's explicit session-only disclosure.
        # A locked OS store must not look like proof that no saved copy exists.
        storage_warning = (
            "This connection is available for this session. Previously saved OS credentials, "
            "if any, were not checked or removed. Unlock the credential store and remove the "
            "connection to clear a saved copy."
            if any(
                runtime.settings._key(scope["id"], kind) in runtime.settings.unverified_persistent
                for kind in ("model", "qbo")
            )
            else runtime.settings.storage_warning
        )

        def mode(kind):
            value = runtime.settings.storage_modes.get(runtime.settings._key(scope["id"], kind))
            return (
                "os-credential-store"
                if value == "persistent"
                else "session-only"
                if value == "session-only"
                else None
            )

        return {
            "storage_warning": storage_warning,
            "model": {
                "configured": bool(model),
                "provider": model.get("provider") if model else None,
                "model": model.get("model") if model else None,
                "storage_mode": mode("model"),
                "consent": bool(model and model.get("consent")),
            },
            "qbo": {
                "configured": bool(qbo),
                "bound_company": qbo.get("bound_company") if qbo else None,
                "storage_mode": mode("qbo"),
                "environment": qbo.get("environment") if qbo else None,
            },
        }

    @app.post("/local/connections/model")
    async def save_model(request: Request):
        scope = runtime.require_scope(request)
        body = await request.json()
        if body.get("provider") not in {"openai", "anthropic"}:
            raise HTTPException(400, "Choose OpenAI or Anthropic.")
        key = runtime.settings._text(body.get("api_key"), "API key", 1000)
        model = runtime.settings._text(body.get("model"), "model name", 100)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,99}", model):
            raise HTTPException(400, "Invalid model name.")
        if body.get("consent") is not True:
            raise HTTPException(400, "Accept the provider data-transfer and usage notice first.")
        with runtime.financial_lock:
            runtime.workspaces.require_current(scope)
            mode = runtime.settings.save(
                scope["id"],
                "model",
                {"provider": body["provider"], "api_key": key, "model": model, "consent": True},
                body.get("persist") is True,
            )
        return {"ok": True, "storage_mode": mode}

    @app.post("/local/connections/model/test")
    def test_model(request: Request):
        from .ai import test_connection

        scope = runtime.require_scope(request)
        bundle = runtime.settings.get(scope["id"], "model")
        if not bundle:
            raise HTTPException(409, "Save an AI connection in this workspace first.")
        return test_connection(runtime, scope, bundle)

    @app.delete("/local/connections/model")
    def delete_model(request: Request):
        scope = runtime.require_scope(request)
        runtime.settings.remove(scope["id"], "model")
        return {"ok": True}

    @app.post("/local/connections/qbo")
    async def save_qbo(request: Request):
        scope = runtime.require_scope(request)
        if scope["synthetic"]:
            raise HTTPException(400, "Create your own entity before connecting QuickBooks.")
        body = await request.json()
        if not scope["realm_id"].startswith("local-") and body.get("realm_id") != scope["realm_id"]:
            raise HTTPException(409, "Use a new workspace for a different company.")
        mode = runtime.settings.set_qbo(scope["id"], body)
        return {"ok": True, "storage_mode": mode}

    @app.post("/local/connections/qbo/test")
    async def test_qbo(request: Request):
        scope = runtime.require_scope(request)
        body = await request.json()
        if scope["synthetic"]:
            raise HTTPException(400, "Synthetic workspaces use labelled fictional data only.")
        with runtime.financial_lock:
            client = QBOClient(runtime.settings, scope["id"], lock=runtime.financial_lock)
            company = client.company()
            runtime.workspaces.require_current(scope)
            if body.get("confirm_company_id") is not None:
                if str(body["confirm_company_id"]) != company["id"]:
                    raise HTTPException(409, "The returned company differs from the confirmation.")
                updated = runtime.workspaces.bind_realm(scope, company["id"])
                client.bind(company["id"])
                return {"ok": True, "company": company, "requires_confirmation": False, "workspace": updated}
            return {"ok": True, "company": company, "requires_confirmation": True}

    @app.delete("/local/connections/qbo")
    def delete_qbo(request: Request):
        scope = runtime.require_scope(request)
        runtime.settings.remove(scope["id"], "qbo")
        return {"ok": True}

    @app.exception_handler(SettingsError)
    async def settings_error(request, error):
        from starlette.responses import JSONResponse

        return JSONResponse({"detail": str(error)}, status_code=400)

    @app.exception_handler(QBOError)
    async def qbo_error(request, error):
        from starlette.responses import JSONResponse

        return JSONResponse({"detail": str(error)}, status_code=400)

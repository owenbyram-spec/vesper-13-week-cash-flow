"""Common local-only API routes shared by each independently packaged app."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import threading
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .assistant import AssistantError, ResponsesAdapter, run_assistant
from .connectors import QBOClient, QBOError
from .jobs import PollingJob
from .security import LocalSecurityMiddleware
from .settings import MemorySecretStore, SettingsError, SettingsService


@dataclass
class CommonServices:
    settings: SettingsService
    polling: PollingJob
    qbo_factory: object
    adapter_factory: object

    @property
    def jobs(self):
        return self.polling


def install_common(
    app: FastAPI,
    *,
    data_dir: Path,
    port: int,
    workspace_getter,
    tools_factory,
    qbo_sync,
    testing: bool = False,
) -> CommonServices:
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    settings = SettingsService(data_dir, MemorySecretStore() if testing else None, testing=testing)
    qbo_lock = threading.RLock()
    state_lock = threading.RLock()
    active_runs = {}
    run_file = data_dir / "assistant-runs.jsonl"

    def workspace(required=True):
        scope = workspace_getter()
        if not isinstance(scope, dict) or not isinstance(scope.get("id"), str) or not scope["id"]:
            if required:
                raise HTTPException(409, "Create or select a workspace first.")
            return None
        return dict(scope)

    def same_scope(saved):
        now = workspace(False)
        fields = (
            "id",
            "version",
            "session_id",
            "input_version",
            "version_id",
            "input_hash",
            "result_hash",
            "account_id",
            "period_start",
            "period_end",
        )
        return bool(
            now and all(now.get(key) == saved.get(key) for key in fields if key in now or key in saved)
        )

    def displayed_scope(request: Request):
        current = workspace()
        try:
            raw = request.headers.get("X-Vesper-Scope", "")
            if len(raw) > 4000:
                raise ValueError()
            displayed = json.loads(raw)
            if not isinstance(displayed, dict) or displayed != current:
                raise ValueError()
        except (ValueError, TypeError):
            raise HTTPException(
                409, "The displayed workspace or version changed. Refresh this page before continuing."
            ) from None
        # Use the snapshot that passed validation; never select scope from a request.
        return current

    def execute_sync(params, workspace_id, captured=None):
        scope = captured or workspace(False)
        if captured and not same_scope(captured):
            raise HTTPException(409, "The displayed workspace or version changed before refresh started.")
        if not scope or scope["id"] != workspace_id:
            polling.stop()
            raise QBOError(
                "The active workspace changed. Polling stopped; select and reconnect the intended workspace."
            )
        client = services.qbo_factory(scope["id"])
        client._require_bound()
        result = qbo_sync(client, scope, params)
        # The callback must validate its captured scope/version immediately before commit.
        return result

    polling = PollingJob(execute_sync)
    services = CommonServices(
        settings,
        polling,
        lambda workspace_id: QBOClient(settings, workspace_id, lock=qbo_lock),
        lambda bundle: ResponsesAdapter(api_key=bundle["api_key"], model=bundle["model"]),
    )
    app.state.common = services
    app.add_middleware(LocalSecurityMiddleware, port=port, testing=testing)

    @app.exception_handler(RequestValidationError)
    async def safe_validation(_request, _exc):
        # FastAPI's normal validation envelope can echo secrets supplied in a bad body.
        return JSONResponse(
            {"detail": "The request format is invalid. Review the form and try again."},
            status_code=422,
        )

    @app.exception_handler(SettingsError)
    @app.exception_handler(QBOError)
    @app.exception_handler(AssistantError)
    async def safe_domain_error(_request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.get("/api/bootstrap")
    def bootstrap(request: Request):
        return {
            "csrf_token": request.state.vesper_csrf,
            "local_only": True,
            "workspace": workspace(False),
        }

    @app.get("/api/settings")
    def get_settings(request: Request):
        scope = workspace(False)
        if scope:
            scope = displayed_scope(request)
        if not scope:
            polling.stop()
            return {
                "workspace_required": True,
                "model": {"configured": False},
                "qbo": {"configured": False},
                "polling": polling.status(),
            }
        status = polling.status()
        if status["enabled"] and status["workspace_id"] != scope["id"]:
            polling.stop()
        return {
            **settings.public(scope["id"]),
            "workspace_required": False,
            "polling": polling.status(),
        }

    @app.post("/api/settings/model")
    def set_model(body: dict, request: Request):
        scope = displayed_scope(request)
        storage = settings.set_model(scope["id"], body)
        return {"ok": True, "storage": storage, **settings.public(scope["id"])}

    @app.delete("/api/settings/model")
    def remove_model(request: Request):
        scope = displayed_scope(request)
        with state_lock:
            if scope["id"] in active_runs:
                active_runs[scope["id"]].set()
        settings.remove(scope["id"], "model")
        return {"ok": True, **settings.public(scope["id"])}

    @app.post("/api/settings/model/test")
    def test_model(request: Request):
        scope = displayed_scope(request)
        bundle = settings.get(scope["id"], "model")
        if not bundle:
            raise SettingsError("Save an AI connection in this workspace first.")
        # Explicit user action; minimal synthetic prompt only, and disclosed as billable.
        services.adapter_factory(bundle).respond(
            [{"role": "user", "content": "Reply with the word connected."}], [], 20
        )
        return {
            "ok": True,
            "message": "The model connection responded. This test can incur API usage.",
            "model": bundle["model"],
        }

    @app.post("/api/settings/qbo")
    def set_qbo(body: dict, request: Request):
        scope = displayed_scope(request)
        if polling.status()["running"]:
            raise HTTPException(409, "Wait for the current refresh before replacing the connection.")
        polling.stop()
        with qbo_lock:
            storage = settings.set_qbo(scope["id"], body)
        return {
            "ok": True,
            "storage": storage,
            "message": "Connection saved. Test it, then confirm the displayed company.",
            **settings.public(scope["id"]),
        }

    @app.post("/api/settings/qbo/test")
    def test_qbo(request: Request, body: dict | None = None):
        scope = displayed_scope(request)
        client = services.qbo_factory(scope["id"])
        company = client.company()
        if not same_scope(scope):
            raise HTTPException(409, "The workspace changed. Test again in the selected workspace.")
        confirm = (body or {}).get("confirm_company_id")
        if confirm is not None:
            company = client.bind(confirm)
            return {
                "ok": True,
                "company": company,
                "requires_confirmation": False,
                "message": "This company is bound to the current local workspace.",
            }
        return {
            "ok": True,
            "company": company,
            "requires_confirmation": True,
            "message": "Confirm that this is the intended company before reading its financial data.",
        }

    @app.delete("/api/settings/qbo")
    def disconnect_qbo(request: Request):
        scope = displayed_scope(request)
        polling.stop()
        # Drain an in-flight refresh so it cannot restore rotated credentials after disconnect.
        with polling.run_lock:
            with qbo_lock:
                settings.remove(scope["id"], "qbo")
        return {
            "ok": True,
            "message": "Local QBO credentials removed and polling stopped. Existing imported data is retained. Revoke the grant in Intuit to revoke provider access.",
        }

    @app.get("/api/qbo/accounts")
    def accounts(request: Request):
        scope = displayed_scope(request)
        result = services.qbo_factory(scope["id"]).accounts()
        if not same_scope(scope):
            raise HTTPException(409, "The workspace changed during the read.")
        return {
            "accounts": [
                {
                    "id": str(a["Id"]),
                    "name": str(a.get("FullyQualifiedName", a.get("Name", "Unnamed")))[:250],
                    "type": a.get("AccountType"),
                    "currency": a.get("CurrencyRef", {}).get("value"),
                    "parent_id": a.get("ParentRef", {}).get("value"),
                }
                for a in result
                if a.get("AccountType") == "Bank"
            ]
        }

    @app.post("/api/qbo/sync")
    def sync(body: dict, request: Request):
        scope = displayed_scope(request)
        if len(json.dumps(body)) > 10000:
            raise HTTPException(400, "Refresh parameters exceed the allowed size.")
        # params is the documented envelope; accept a direct params dict for straightforward clients.
        params = body.get("params", body)
        if not isinstance(params, dict):
            raise HTTPException(400, "Refresh parameters must be an object.")
        try:
            return {
                "ok": True,
                "result": polling.run(
                    params, scope["id"], callback=lambda p, wid: execute_sync(p, wid, captured=scope)
                ),
                "polling": polling.status(),
            }
        except (QBOError, SettingsError, HTTPException):
            raise
        except Exception:
            raise HTTPException(
                400,
                "The refresh did not pass source or workflow controls. Previous valid data is unchanged.",
            ) from None

    @app.post("/api/qbo/polling")
    def configure_polling(body: dict, request: Request):
        scope = displayed_scope(request)
        enabled = body.get("enabled")
        if type(enabled) is not bool or not isinstance(body.get("params", {}), dict):
            raise HTTPException(400, "Choose enabled true or false and valid refresh parameters.")
        if enabled:
            services.qbo_factory(scope["id"])._require_bound()
        try:
            status = polling.configure(
                enabled=enabled,
                interval_seconds=body.get("interval_seconds", 300),
                params=body.get("params", {}),
                workspace_id=scope["id"],
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        return {"ok": True, "polling": status}

    def record_run(run_id, scope, status, result=None):
        entry = {
            "id": run_id,
            "at": datetime.now(timezone.utc).isoformat(),
            "workspace_hash": sha256(scope["id"].encode()).hexdigest(),
            "status": status,
            "mode": result.get("mode") if result else None,
            "evidence_ids": [e["id"] for e in result.get("evidence", [])] if result else [],
            "proposal_ids": [
                str(p.get("id", p.get("proposal_id", "draft")))[:180] for p in result.get("proposals", [])
            ]
            if result
            else [],
        }
        with state_lock:
            with run_file.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, separators=(",", ":")) + "\n")

    @app.post("/api/assistant")
    def assistant(body: dict, request: Request):
        scope = displayed_scope(request)
        rehearsal = body.get("rehearsal", False)
        if type(rehearsal) is not bool:
            raise AssistantError("Choose an explicit assistant mode.")
        if rehearsal and scope.get("synthetic") is not True:
            raise AssistantError("Synthetic rehearsal is available only in a labelled synthetic workspace.")
        message = body.get("message", "")
        cancelled = threading.Event()
        with state_lock:
            if scope["id"] in active_runs:
                raise HTTPException(409, "An assistant run is already active. Cancel it or wait.")
            active_runs[scope["id"]] = cancelled
        run_id = "run-" + uuid.uuid4().hex
        record_run(run_id, scope, "started")
        try:
            bundle = None if rehearsal else settings.get(scope["id"], "model")
            result = run_assistant(
                message=message,
                tools=tools_factory(scope),
                rehearsal=rehearsal,
                settings=bundle,
                adapter=services.adapter_factory(bundle) if bundle else None,
                cancelled=cancelled,
                scope_check=lambda: same_scope(scope),
            )
            record_run(run_id, scope, "succeeded", result)
            return {**result, "run_id": run_id}
        except (AssistantError, SettingsError, HTTPException):
            record_run(run_id, scope, "cancelled" if cancelled.is_set() else "failed")
            raise
        except Exception:
            record_run(run_id, scope, "failed")
            raise HTTPException(
                400,
                "The assistant could not complete this run. Your workflow is saved.",
            ) from None
        finally:
            with state_lock:
                active_runs.pop(scope["id"], None)

    @app.post("/api/assistant/cancel")
    def cancel_assistant(request: Request):
        scope = displayed_scope(request)
        with state_lock:
            event = active_runs.get(scope["id"])
            if event:
                event.set()
        return {
            "ok": True,
            "message": "Cancellation requested. An in-flight provider request may finish before the run stops.",
        }

    previous_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def common_lifespan(application):
        async with previous_lifespan(application) as state:
            try:
                yield state
            finally:
                polling.stop()
                with state_lock:
                    for event in active_runs.values():
                        event.set()

    app.router.lifespan_context = common_lifespan
    return services

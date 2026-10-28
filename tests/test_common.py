import json
from fastapi import FastAPI
from fastapi.testclient import TestClient

from vesper_cashflow.assistant import Tool
from vesper_cashflow.common import install_common
from vesper_cashflow.jobs import PollingJob


def configured(tmp_path):
    app = FastAPI()
    scope = {"id": "synthetic-a", "synthetic": True, "currency": "USD", "version": 1}

    def tools(s):
        return [
            Tool(
                "read_summary",
                "Read deterministic current evidence",
                {
                    "type": "object",
                    "properties": {},
                    "required": [],
                    "additionalProperties": False,
                },
                lambda a: {"evidence_id": "synthetic-v1", "amount": "10.00"},
                {},
            )
        ]

    common = install_common(
        app,
        data_dir=tmp_path,
        port=8765,
        workspace_getter=lambda: scope or None,
        tools_factory=tools,
        qbo_sync=lambda c, s, p: {"complete": True},
        testing=True,
    )

    @app.get("/")
    def home():
        return {"ok": True}

    client = TestClient(app)
    client.get("/")
    csrf = client.get("/api/bootstrap").json()["csrf_token"]
    headers = {"Origin": "http://testserver", "X-Vesper-CSRF": csrf, "X-Vesper-Scope": json.dumps(scope)}
    client.headers["X-Vesper-Scope"] = json.dumps(scope)
    return client, headers, scope, common


def test_common_rehearsal_settings_scopes_and_sanitized_run_log(tmp_path):
    client, headers, scope, common = configured(tmp_path)
    result = client.post(
        "/api/assistant",
        json={"message": "Explain", "rehearsal": True},
        headers=headers,
    )
    assert result.status_code == 200 and result.json()["mode"] == "synthetic-rehearsal"
    assert common.settings.get("synthetic-a", "model") is None
    body = {
        "api_key": "synthetic-secret",
        "model": "synthetic-model",
        "consent": True,
        "persist": False,
    }
    saved = client.post("/api/settings/model", json=body, headers=headers)
    assert saved.status_code == 200 and "synthetic-secret" not in saved.text
    assert "synthetic-secret" not in client.get("/api/settings").text
    log = (tmp_path / "assistant-runs.jsonl").read_text()
    assert "synthetic-secret" not in log and "Explain" not in log
    scope["id"] = "synthetic-b"
    client.headers["X-Vesper-Scope"] = json.dumps(scope)
    assert client.get("/api/settings").json()["model"]["configured"] is False
    scope["synthetic"] = False
    headers["X-Vesper-Scope"] = json.dumps(scope)
    client.headers["X-Vesper-Scope"] = json.dumps(scope)
    rejected = client.post(
        "/api/assistant",
        json={"message": "Explain", "rehearsal": True},
        headers=headers,
    )
    assert rejected.status_code == 400
    scope.clear()
    assert client.get("/api/settings").json()["workspace_required"] is True
    assert client.post("/api/settings/model/test", headers=headers).status_code == 409


def test_secret_body_validation_never_echoes_input(tmp_path):
    client, headers, _, _ = configured(tmp_path)
    response = client.post("/api/settings/model", json=["synthetic-secret"], headers=headers)
    assert response.status_code == 422 and "synthetic-secret" not in response.text


def test_polling_serial_failure_visibility_and_shutdown():
    called = []

    def refresh(params, scope):
        called.append((params, scope))
        if params.get("fail"):
            raise ValueError("secret-provider-message")
        return {"good": True}

    polling = PollingJob(refresh)
    assert polling.run({}, "a") == {"good": True}
    before = polling.status()["last_success"]
    try:
        polling.run({"fail": True}, "a")
    except ValueError:
        pass
    status = polling.status()
    assert status["last_success"] == before and "secret-provider-message" not in str(status)
    polling.run_lock.acquire()
    try:
        try:
            polling.run({}, "a")
            assert False
        except ValueError:
            pass
    finally:
        polling.run_lock.release()
    status = polling.configure(enabled=True, interval_seconds=60, params={}, workspace_id="a")
    assert status["enabled"]
    polling.stop()
    assert not polling.status()["enabled"]


def test_stale_session_or_input_rejects_model_result(tmp_path):
    for changed_field in ("session_id", "input_version", "input_hash", "account_id"):
        client, headers, scope, common = configured(tmp_path / changed_field)
        common.settings.set_model(
            scope["id"], {"api_key": "synthetic", "model": "synthetic", "consent": True}
        )
        scope[changed_field] = "before"
        headers["X-Vesper-Scope"] = json.dumps(scope)

        class ChangedScopeAdapter:
            def respond(self, *args):
                scope[changed_field] = "after"
                return {"output": []}

        common.adapter_factory = lambda bundle: ChangedScopeAdapter()
        result = client.post("/api/assistant", json={"message": "Explain"}, headers=headers)
        assert result.status_code == 400 and "changed" in result.json()["detail"]


def test_explicit_company_confirmation_and_disconnect_stop_polling(tmp_path):
    from vesper_cashflow.connectors import QBOError

    client, headers, scope, common = configured(tmp_path)

    class MockQBO:
        bound = False

        def company(self):
            return {"id": "123", "name": "Synthetic Company", "environment": "sandbox"}

        def bind(self, company_id):
            if company_id != "123":
                raise QBOError("Company confirmation did not match.")
            self.bound = True
            return self.company()

        def _require_bound(self):
            if not self.bound:
                raise QBOError("Confirm the displayed company first.")

        def accounts(self):
            self._require_bound()
            return [{"Id": "10", "Name": "Synthetic Bank", "AccountType": "Bank"}]

    fake = MockQBO()
    common.qbo_factory = lambda wid: fake
    assert client.get("/api/qbo/accounts").status_code == 400
    tested = client.post("/api/settings/qbo/test", headers=headers)
    assert tested.json()["requires_confirmation"] is True
    assert not fake.bound
    assert (
        client.post(
            "/api/settings/qbo/test",
            json={"confirm_company_id": "999"},
            headers=headers,
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/api/settings/qbo/test",
            json={"confirm_company_id": "123"},
            headers=headers,
        ).json()["requires_confirmation"]
        is False
    )
    assert client.get("/api/qbo/accounts").json()["accounts"][0]["id"] == "10"
    enabled = client.post(
        "/api/qbo/polling",
        json={"enabled": True, "interval_seconds": 60},
        headers=headers,
    )
    assert enabled.json()["polling"]["enabled"]
    assert client.delete("/api/settings/qbo", headers=headers).status_code == 200
    assert not common.polling.status()["enabled"]


def test_common_lifespan_preserves_existing_startup_shutdown(tmp_path):
    from contextlib import asynccontextmanager

    events = []

    @asynccontextmanager
    async def lifespan(app):
        events.append("started")
        yield
        events.append("stopped")

    app = FastAPI(lifespan=lifespan)
    common = install_common(
        app,
        data_dir=tmp_path,
        port=8765,
        workspace_getter=lambda: {"id": "a"},
        tools_factory=lambda s: [],
        qbo_sync=lambda c, s, p: {},
        testing=True,
    )
    with TestClient(app):
        common.polling.configure(enabled=True, interval_seconds=60, params={}, workspace_id="a")
        assert events == ["started"]
    assert events == ["started", "stopped"] and not common.polling.status()["enabled"]


def test_displayed_scope_required_and_stale_settings_cannot_change_other_workspace(tmp_path):
    client, headers, scope, common = configured(tmp_path)
    body = {"api_key": "synthetic-secret-a", "model": "synthetic", "consent": True, "persist": False}
    assert client.post("/api/settings/model", json=body, headers=headers).status_code == 200
    old = dict(headers)
    scope["id"] = "synthetic-b"
    common.settings.set_model("synthetic-b", {**body, "api_key": "synthetic-secret-b"})
    for method, path, payload in [
        ("post", "/api/settings/model", body),
        ("delete", "/api/settings/model", None),
        ("post", "/api/assistant", {"message": "Explain", "rehearsal": True}),
        ("post", "/api/qbo/sync", {}),
    ]:
        kwargs = {"headers": old}
        if payload is not None:
            kwargs["json"] = payload
        assert getattr(client, method)(path, **kwargs).status_code == 409
    assert common.settings.get("synthetic-b", "model")["api_key"] == "synthetic-secret-b"
    assert common.settings.get("synthetic-a", "model")["api_key"] == "synthetic-secret-a"
    client.headers.pop("X-Vesper-Scope", None)
    missing = {k: v for k, v in headers.items() if k != "X-Vesper-Scope"}
    assert client.post("/api/settings/model", json=body, headers=missing).status_code == 409
    fresh = {**headers, "X-Vesper-Scope": json.dumps(scope)}
    assert client.get("/api/settings", headers=fresh).status_code == 200

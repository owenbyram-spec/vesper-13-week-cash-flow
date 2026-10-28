"""Model-save response contract using isolated memory secrets and no provider."""

import json

from fastapi.testclient import TestClient
import pytest

from vesper_cashflow.web import create_app


class DisplayedClient(TestClient):
    def request(self, method, url, **kwargs):
        if str(url).startswith("/api/") and str(url) != "/api/state":
            headers = dict(kwargs.get("headers") or {})
            if not any(key.lower() == "x-vesper-scope" for key in headers):
                scope = super().request("GET", "/api/state").json()["scope"]
                headers["X-Vesper-Scope"] = json.dumps(scope)
            kwargs["headers"] = headers
        return super().request(method, url, **kwargs)


@pytest.fixture
def local_model(tmp_path):
    app = create_app(tmp_path, port=8766, testing=True)
    calls = []

    def adapter_factory(bundle):
        class Adapter:
            def respond(self, messages, tools, remaining_seconds):
                calls.append({"bundle": dict(bundle), "messages": messages, "tools": tools})
                return {"output": []}

        return Adapter()

    app.state.common.adapter_factory = adapter_factory
    with DisplayedClient(app, base_url="http://127.0.0.1:8766") as client:
        client.get("/").raise_for_status()
        csrf = client.get("/api/bootstrap").json()["csrf_token"]
        client.headers.update({"Origin": "http://127.0.0.1:8766", "X-Vesper-CSRF": csrf})
        client.post("/api/demo", json={}).raise_for_status()
        yield client, calls


def connection(**extra):
    return {
        "api_key": "fixture-model-save-secret",
        "model": "fixture-model",
        "consent": True,
        "persist": False,
        **extra,
    }


def test_model_save_returns_configuration_and_test_uses_saved_bundle(local_model):
    client, calls = local_model
    assert client.get("/api/settings").json()["model"]["configured"] is False
    payload = connection()
    saved = client.post("/api/settings/model", json=payload)
    assert saved.status_code == 200, saved.text
    assert saved.json()["ok"] is True
    assert saved.json()["storage_mode"] == "session-only"
    assert "OS credential store" in saved.json()["storage"]
    assert saved.json()["model"] == {
        "configured": True, "model": payload["model"], "consent": True,
    }
    current = client.get("/api/settings")
    assert current.status_code == 200
    assert current.json()["model"] == saved.json()["model"]
    tested = client.post("/api/settings/model/test", json={})
    assert tested.status_code == 200, tested.text
    assert tested.json()["ok"] is True
    assert tested.json()["model"] == payload["model"]
    assert calls == [{
        "bundle": {key: payload[key] for key in ("api_key", "model", "consent")},
        "messages": [{"role": "user", "content": "Reply with the word connected."}],
        "tools": [],
    }]
    assert all(payload["api_key"] not in response.text for response in (saved, current, tested))


@pytest.mark.parametrize("invalid", [{"model": "invalid model name"}, {"consent": False}])
def test_failed_save_neither_creates_nor_replaces_valid_connection(local_model, invalid):
    client, calls = local_model
    rejected_payload = connection(api_key="fixture-rejected-save-secret", **invalid)
    initial_failure = client.post("/api/settings/model", json=rejected_payload)
    assert initial_failure.status_code == 400
    assert client.get("/api/settings").json()["model"]["configured"] is False
    unconfigured_test = client.post("/api/settings/model/test", json={})
    assert unconfigured_test.status_code == 400
    assert "Save an AI connection" in unconfigured_test.json()["detail"]
    assert calls == []

    accepted_payload = connection()
    accepted = client.post("/api/settings/model", json=accepted_payload)
    assert accepted.status_code == 200
    failed_replacement = client.post("/api/settings/model", json=rejected_payload)
    assert failed_replacement.status_code == 400
    current = client.get("/api/settings")
    assert current.json()["model"] == accepted.json()["model"]
    assert client.post("/api/settings/model/test", json={}).status_code == 200
    assert calls[0]["bundle"]["api_key"] == accepted_payload["api_key"]
    for response in (initial_failure, unconfigured_test, accepted, failed_replacement, current):
        assert accepted_payload["api_key"] not in response.text
        assert rejected_payload["api_key"] not in response.text


def test_stale_workspace_save_and_test_cannot_change_other_connection(local_model):
    client, calls = local_model
    first = client.get("/api/state").json()
    accepted_payload = connection()
    assert client.post("/api/settings/model", json=accepted_payload).status_code == 200
    client.post("/api/demo", json={}).raise_for_status()
    second = client.get("/api/state").json()
    assert second["workspace"]["id"] != first["workspace"]["id"]
    assert second["version"]["input_hash"] == first["version"]["input_hash"]
    stale = {"X-Vesper-Scope": json.dumps(first["scope"])}
    rejected_payload = connection(api_key="fixture-foreign-save-secret")
    for route, payload in (("/api/settings/model", rejected_payload), ("/api/settings/model/test", {})):
        rejected = client.post(route, json=payload, headers=stale)
        assert rejected.status_code == 409
        assert rejected_payload["api_key"] not in rejected.text
    assert calls == []
    assert client.get("/api/settings").json()["model"]["configured"] is False
    client.post("/api/workspaces/activate", json={"id": first["workspace"]["id"]}).raise_for_status()
    assert client.get("/api/settings").json()["model"]["configured"] is True
    assert client.post("/api/settings/model/test", json={}).status_code == 200
    assert calls[0]["bundle"]["api_key"] == accepted_payload["api_key"]


def test_qbo_save_reports_selected_mode_without_overwriting_public_storage(local_model):
    client, calls = local_model
    payload = {
        "client_id": "fixture-intuit-client",
        "client_secret": "fixture-intuit-save-secret",
        "realm_id": "123456",
        "refresh_token": "fixture-intuit-refresh-secret",
        "environment": "sandbox",
        "persist": False,
    }
    saved = client.post("/api/settings/qbo", json=payload)
    assert saved.status_code == 200, saved.text
    assert saved.json()["storage_mode"] == "session-only"
    assert "OS credential store" in saved.json()["storage"]
    assert saved.json()["qbo"] == {
        "configured": True, "environment": "sandbox", "bound": False, "company": None,
    }
    current = client.get("/api/settings")
    assert current.status_code == 200
    assert current.json()["qbo"] == saved.json()["qbo"]
    assert calls == []
    for response in (saved, current):
        assert payload["client_secret"] not in response.text
        assert payload["refresh_token"] not in response.text

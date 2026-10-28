"""HTTP proofs of the actual installable app with isolated synthetic data."""

from copy import deepcopy
from datetime import datetime
import json

import pytest
from fastapi.testclient import TestClient

from vesper_cashflow.web import create_app


class DisplayedClient(TestClient):
    """Ordinary calls use freshly displayed scope; explicit headers test stale tabs."""

    def request(self, method, url, **kwargs):
        if str(url).startswith("/api/") and str(url) != "/api/state":
            headers = dict(kwargs.get("headers") or {})
            if not any(key.lower() == "x-vesper-scope" for key in headers):
                current = super().request("GET", "/api/state").json().get("scope")
                headers["X-Vesper-Scope"] = json.dumps(current)
            kwargs["headers"] = headers
        return super().request(method, url, **kwargs)


@pytest.fixture
def local(tmp_path):
    app = create_app(tmp_path, port=8766, testing=True)
    with DisplayedClient(app, base_url="http://127.0.0.1:8766") as client:
        assert client.get("/").status_code == 200
        csrf = client.get("/api/bootstrap").json()["csrf_token"]
        client.headers.update({"Origin": "http://127.0.0.1:8766", "X-Vesper-CSRF": csrf})
        yield app, client


def test_complete_synthetic_setup_scenario_approve_export_resume(local):
    app, client = local
    assert client.get("/api/state").json()["workspace"] is None
    demo = client.post("/api/demo", json={})
    assert demo.status_code == 200, demo.text
    base = demo.json()
    assert base["result"]["ending_cash"] == "35000.00"
    rehearsal = client.post(
        "/api/assistant", json={"message": "Explain the lowest cash and collection delay", "rehearsal": True}
    )
    assert rehearsal.status_code == 200, rehearsal.text
    assert rehearsal.json()["mode"] == "synthetic-rehearsal"
    assert rehearsal.json()["usage"]["provider_calls"] == 0
    assert len(rehearsal.json()["evidence"]) == 3
    assert app.state.store.get_version()["id"] == base["id"]
    proposal = client.post(
        "/api/proposals",
        json={"delay_days": 14, "label": "Delay collections", "base_hash": base["input_hash"]},
    )
    assert proposal.status_code == 200, proposal.text
    exact = proposal.json()
    assert exact["preview"]["minimum_cash"]["amount"] == "15500.00"
    approved = client.post(
        "/api/proposals/resolve",
        json={"id": exact["id"], "proposal_hash": exact["proposal_hash"], "approve": True},
    )
    assert approved.status_code == 200, approved.text
    new = approved.json()["version"]
    for kind, content in [("csv", "text/csv"), ("xlsx", "spreadsheetml"), ("print", "text/html")]:
        response = client.get("/api/export/" + kind, params={"version_id": base["id"]})
        assert response.status_code == 200, response.text
        assert content in response.headers["content-type"]
    backup = client.get("/api/backup").json()
    restored = client.post("/api/restore", json=backup)
    assert restored.status_code == 200, restored.text
    assert restored.json()["id"] != backup["workspace"]["id"]
    current = client.get("/api/state").json()
    assert current["version"]["result"] == new["result"]
    assert len(current["versions"]) == 2


def test_server_scope_and_source_fields_cannot_be_widened(local):
    app, client = local
    base = client.post("/api/demo", json={}).json()
    evil = deepcopy(base["inputs"])
    evil["opening_cash"] = "999999.00"
    response = client.post("/api/assumptions/preview", json={"inputs": evil, "base_hash": base["input_hash"]})
    assert response.status_code == 400 and "new source import" in response.text
    client.post("/api/workspaces", json={"name": "Own separate entity", "currency": "USD"}).json()
    assert client.get("/api/export/csv", params={"version_id": base["id"]}).status_code == 400
    assert (
        client.post(
            "/api/proposals/resolve", json={"id": "foreign", "proposal_hash": "0" * 64, "approve": True}
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/api/assistant", json={"message": "read another workspace", "rehearsal": True}
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/api/workspaces", json={"name": "Name", "currency": "USD", "synthetic": True}
        ).status_code
        == 400
    )


def test_model_tool_loop_uses_real_adapter_contract_without_network(local):
    app, client = local
    base = client.post("/api/demo", json={}).json()

    class Adapter:
        def respond(self, messages, tools, remaining_seconds=20):
            outputs = [entry for entry in messages if entry.get("type") == "function_call_output"]
            if not outputs:
                return {
                    "output": [
                        {
                            "type": "function_call",
                            "name": "forecast_summary",
                            "call_id": "call-synthetic",
                            "arguments": "{}",
                        }
                    ]
                }
            result = json.loads(outputs[-1]["output"])
            return {
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": json.dumps(
                                    {
                                        "summary": "The cash floor remains positive under the reviewed assumptions.",
                                        "evidence_ids": [result["evidence_id"]],
                                    }
                                ),
                            }
                        ],
                    }
                ]
            }

    client.post(
        "/api/settings/model",
        json={
            "api_key": "synthetic-fixture-key",
            "model": "synthetic-model",
            "persist": False,
            "consent": True,
        },
    )
    app.state.common.adapter_factory = lambda bundle: Adapter()
    response = client.post("/api/assistant", json={"message": "Explain the cash floor", "rehearsal": False})
    assert response.status_code == 200, response.text
    assert response.json()["mode"] == "model"
    assert response.json()["evidence"][0]["data"]["forecast"]["opening_cash"] == "25000.00"
    assert app.state.store.get_version()["id"] == base["id"]


def qbo_fixture(today):
    return {
        "opening_cash": "10000.00",
        "currency": "USD",
        "items": [
            {
                "id": "invoice-1",
                "direction": "in",
                "amount": "2500.00",
                "due_date": today,
                "name": "Synthetic QBO invoice",
                "source": {"provider": "QBO", "entity": "Invoice", "id": "1", "as_of": today},
            }
        ],
        "coverage": {key: {"complete": True} for key in ("cash", "accounts", "invoice", "bill")},
        "source": {"complete": True, "as_of": today, "observation": "synthetic fixture"},
    }


def test_qbo_current_date_undated_preserve_prior_on_partial_and_scope_switch(local):
    app, client = local
    own = client.post("/api/workspaces", json={"name": "Own current cash forecast", "currency": "USD"}).json()
    today = datetime.now().astimezone().date().isoformat()

    class Client:
        def _require_bound(self):
            return None

        def forecast_inputs(self, as_of):
            return qbo_fixture(as_of)

    app.state.common.qbo_factory = lambda workspace_id: Client()
    sync = client.post("/api/qbo/sync", json={"as_of": today})
    assert sync.status_code == 200, sync.text
    version = app.state.store.get_version()
    assert version["inputs"]["items"][0]["expected_date"] is None
    assert version["result"]["total_in"] == "0.00"
    assert version["result"]["coverage_complete"] is False
    assert client.post("/api/qbo/sync", json={"as_of": "2020-01-01"}).status_code == 400

    class Partial(Client):
        def forecast_inputs(self, as_of):
            acquired = qbo_fixture(as_of)
            acquired["coverage"]["invoice"]["complete"] = False
            return acquired

    app.state.common.qbo_factory = lambda workspace_id: Partial()
    assert client.post("/api/qbo/sync", json={"as_of": today}).status_code == 400
    assert app.state.store.get_version()["id"] == version["id"]
    other = app.state.store.create_workspace("Another entity")
    app.state.store.activate(own["id"])

    class Switched(Client):
        def forecast_inputs(self, as_of):
            acquired = qbo_fixture(as_of)
            app.state.store.activate(other["id"])
            return acquired

    app.state.common.qbo_factory = lambda workspace_id: Switched()
    assert client.post("/api/qbo/sync", json={"as_of": today}).status_code == 400
    assert app.state.store.active_workspace()["id"] == other["id"]
    assert app.state.store.active_workspace()["active_version"] is None
    app.state.store.activate(own["id"])
    assert app.state.store.get_version()["id"] == version["id"]


def test_model_draft_rechecks_exact_scope_inside_write(local):
    app, client = local
    base = client.post("/api/demo", json={}).json()
    scope = app.state.store.active_workspace()
    # Same input hash in a different immutable version is still a changed base.
    app.state.store.save_version(base["inputs"], "Identical new version", base_hash=base["input_hash"])
    from vesper_cashflow.proposals import delay_receipts

    with pytest.raises(ValueError, match="version changed"):
        app.state.store.draft_proposal(
            delay_receipts(base["inputs"], 14), "Stale draft", base_hash=base["input_hash"], scope=scope
        )
    assert app.state.store.list_proposals() == []


def test_import_preview_is_bound_to_entity_and_exact_base(local):
    from pathlib import Path

    app, client = local
    root = Path(__file__).parents[1] / "examples"
    client.post("/api/workspaces", json={"name": "Entity one", "currency": "USD"})
    request = {
        "opening_csv": (root / "opening-cash.csv").read_text(),
        "items_csv": (root / "cash-items.csv").read_text(),
    }
    preview = client.post("/api/import/preview", json=request).json()
    reviewed = {
        "inputs": preview["inputs"],
        "input_hash": preview["input_hash"],
        "base_hash": None,
        "preview_binding": preview["preview_binding"],
    }
    first = app.state.store.active_workspace()
    client.post("/api/workspaces", json={"name": "Entity two", "currency": "USD"})
    refused = client.post("/api/import", json=reviewed)
    assert refused.status_code == 400 and "Workspace or version changed" in refused.text
    assert app.state.store.active_workspace()["active_version"] is None
    client.post("/api/workspaces/activate", json={"id": first["id"]})
    saved = client.post("/api/import", json=reviewed)
    assert saved.status_code == 200, saved.text
    version = saved.json()
    changed = deepcopy(version["inputs"])
    changed["items"][0]["expected_date"] = "2026-11-05"
    assumption = client.post(
        "/api/assumptions/preview", json={"inputs": changed, "base_hash": version["input_hash"]}
    )
    assert assumption.status_code == 200, assumption.text
    exact = assumption.json()
    response = client.post(
        "/api/assumptions",
        json={key: exact[key] for key in ("inputs", "input_hash", "base_hash", "preview_binding")},
    )
    assert response.status_code == 200, response.text
    assert response.json()["inputs"]["items"][0]["due_date"] == "2026-10-20"
    assert response.json()["inputs"]["items"][0]["expected_date"] == "2026-11-05"


def test_entity_delete_clears_its_local_credentials(local):
    app, client = local
    client.post("/api/demo", json={})
    scope = app.state.store.active_workspace()
    client.post(
        "/api/settings/model",
        json={
            "api_key": "synthetic-fixture-key",
            "model": "synthetic-model",
            "persist": False,
            "consent": True,
        },
    )
    assert app.state.common.settings.get(scope["id"], "model")
    bad = client.request("DELETE", "/api/workspaces/active", json={"confirmation": "wrong"})
    assert bad.status_code == 400
    assert app.state.common.settings.get(scope["id"], "model")
    removed = client.request("DELETE", "/api/workspaces/active", json={"confirmation": scope["name"]})
    assert removed.status_code == 200, removed.text
    assert app.state.common.settings.get(scope["id"], "model") is None
    assert app.state.store.active_workspace() is None


def test_qbo_refresh_preserves_manual_one_time_tax_and_avoids_ar_duplication(local):
    from vesper_cashflow.imports import synthetic_demo

    app, client = local
    client.post(
        "/api/workspaces", json={"name": "Own entity with manual assumptions", "currency": "USD"}
    ).json()
    initial = app.state.store.save_version(synthetic_demo(), "Imported source with manual tax")
    assert any(row["id"] == "tax-1" and row["category"] == "assumption" for row in initial["inputs"]["items"])
    today = datetime.now().astimezone().date().isoformat()

    class Client:
        def _require_bound(self):
            return None

        def forecast_inputs(self, as_of):
            return qbo_fixture(as_of)

    app.state.common.qbo_factory = lambda workspace_id: Client()
    response = client.post("/api/qbo/sync", json={"as_of": today})
    assert response.status_code == 200, response.text
    current = app.state.store.get_version()
    assert {row["id"] for row in current["inputs"]["items"]} == {"tax-1", "invoice-1"}
    assert next(row for row in current["inputs"]["items"] if row["id"] == "tax-1") == next(
        row for row in initial["inputs"]["items"] if row["id"] == "tax-1"
    )
    assert current["inputs"]["coverage"]["taxes"] == "missing"
    assert len(current["inputs"]["recurrences"]) == len(initial["inputs"]["recurrences"])
    # A second refresh preserves the manual input exactly once.
    assert client.post("/api/qbo/sync", json={"as_of": today}).status_code == 200
    assert [row["id"] for row in app.state.store.get_version()["inputs"]["items"]].count("tax-1") == 1


def test_stale_displayed_tab_cannot_edit_equal_hash_other_entity(local):
    from vesper_cashflow.imports import synthetic_demo

    app, client = local
    first = client.post("/api/demo", json={}).json()
    displayed = client.get("/api/state").json()["scope"]
    client.post("/api/workspaces", json={"name": "Equal hash separate entity", "currency": "USD"})
    second = app.state.store.save_version(synthetic_demo(), "Same financial inputs")
    assert first["input_hash"] == second["input_hash"]
    edited = deepcopy(first["inputs"])
    edited["items"][0]["expected_date"] = "2026-11-06"
    stale = {"X-Vesper-Scope": json.dumps(displayed)}
    response = client.post(
        "/api/assumptions/preview", headers=stale, json={"inputs": edited, "base_hash": first["input_hash"]}
    )
    assert response.status_code in (400, 409), response.text
    assert app.state.store.get_version()["id"] == second["id"]
    response = client.post(
        "/api/proposals", headers=stale, json={"delay_days": 14, "base_hash": first["input_hash"]}
    )
    assert response.status_code in (400, 409), response.text
    assert app.state.store.list_proposals() == []


def test_state_response_is_consistent_during_concurrent_workspace_selection(local, monkeypatch):
    import threading

    app, client = local
    first = client.post("/api/demo", json={}).json()
    first_scope = app.state.store.active_workspace()
    other = app.state.store.create_workspace("Concurrent other entity")
    app.state.store.activate(first_scope["id"])
    store = app.state.store
    original = store.list_workspaces
    started, selected = threading.Event(), threading.Event()
    observations = []
    threads = []

    def select_other():
        started.set()
        store.activate(other["id"])
        selected.set()

    def list_with_concurrent_selection():
        thread = threading.Thread(target=select_other)
        threads.append(thread)
        thread.start()
        assert started.wait(1)
        observations.append(selected.wait(0.03))
        return original()

    monkeypatch.setattr(store, "list_workspaces", list_with_concurrent_selection)
    response = client.get("/api/state")
    for thread in threads:
        thread.join(timeout=1)
    assert response.status_code == 200, response.text
    displayed = response.json()
    assert observations == [False]
    assert selected.is_set()
    assert displayed["workspace"]["id"] == first_scope["id"]
    assert displayed["scope"]["id"] == first_scope["id"]
    assert displayed["scope"]["version_id"] == first["id"]
    assert displayed["version"]["workspace_id"] == first_scope["id"]
    assert displayed["version"]["input_hash"] == displayed["scope"]["input_hash"]
    assert store.active_workspace()["id"] == other["id"]

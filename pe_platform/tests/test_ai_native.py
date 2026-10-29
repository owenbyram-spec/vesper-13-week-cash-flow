"""Exercise the real pinned runtime with injected provider responses, never keys.

Each integration scenario runs in its own process because native SQLAlchemy and
the local scope adapters deliberately bind one explicit data directory/process.
"""

from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize(
    "scenario",
    [
        "read",
        "numeric",
        "freshness",
        "proposal",
        "mismatch",
        "stream",
        "stream_errors",
        "memory",
        "scope",
        "catalog",
    ],
)
def test_native_ai_governed_paths_without_provider_access(scenario):
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), scenario],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_forward_minimum_excludes_historical_actuals():
    from pe_platform.backend.proposals import _forecast_comparison

    before = {
        "week_headers": ["2026-10-23", "2026-10-30"],
        "lookback": 1,
        "beginning_balances": [100, 100],
        "total_cash_in": [0, 100],
        "total_cash_out": [0, 0],
        "ending_balances": [100, 200],
    }
    after = {**before, "ending_balances": [100, 150], "total_cash_out": [0, 50]}
    assert _forecast_comparison(before, after)["minimum_cash_delta"] == -50
    with pytest.raises(ValueError):
        _forecast_comparison(before, {**after, "lookback": 0})


def test_client_close_cannot_echo_entered_key():
    from types import SimpleNamespace
    from pe_platform.backend.ai import _ClientProxy

    marker = "arbitrary-entered-dummy-key"

    def fail_close():
        raise RuntimeError("SDK cleanup rejected " + marker)

    proxy = _ClientProxy(SimpleNamespace(close=fail_close), None, {}, {})
    with pytest.raises(RuntimeError) as error:
        proxy.close()
    assert marker not in str(error.value)
    assert str(error.value) == "The selected workspace provider request did not complete"


def _exercise(scenario):
    import json
    import tempfile
    from types import SimpleNamespace as N
    from fastapi.testclient import TestClient
    from pe_platform.backend.main import create_app
    from pe_platform.backend.bootstrap import get_runtime
    from pe_platform.backend import cashflow, proposals

    app = create_app(
        data_dir=Path(tempfile.mkdtemp(prefix="vesper-native-ai-test-")), port=8892, testing=True
    )
    runtime = get_runtime()
    physical_calls = []
    plan = []
    stream_failure = []
    streams_closed = []
    clients_closed = []
    secret_marker = "opaque-fixture-not-a-real-credential"

    class FakeStream:
        def __init__(self, result):
            self.result = result

        @property
        def text_stream(self):
            for part in self.result.content:
                if part.type == "text":
                    yield part.text
                if stream_failure == ["iteration"]:
                    raise RuntimeError("Provider rejected " + secret_marker)

        def __enter__(self):
            if stream_failure == ["enter"]:
                raise RuntimeError("Provider rejected " + secret_marker)
            return self

        def __exit__(self, *_args):
            streams_closed.append(True)
            if stream_failure == ["exit"]:
                raise RuntimeError("Provider rejected " + secret_marker)

        def close(self):
            streams_closed.append(True)

        def get_final_message(self):
            if stream_failure == ["final"]:
                raise RuntimeError("Provider rejected " + secret_marker)
            return self.result

    class FixtureClient:
        def __init__(self, bundle):
            self.provider = bundle["provider"]
            self.chat, self.messages = N(completions=self), self
            self.turn = 0

        def close(self):
            clients_closed.append(True)

        def with_options(self, **_kwargs):
            return self

        def create(self, **kwargs):
            physical_calls.append(kwargs)
            assert kwargs["model"] == "fixture-model"
            if self.provider == "openai":
                assert kwargs.get("store") is False
            if "tools" not in kwargs:
                # Native analysis/context lanes are real, using the same bound
                # provider; do not consume the top-level fixture conversation.
                text, tool = ('{"summary":"Synthetic source context"}', None)
            else:
                text, tool = plan[self.turn]
                self.turn += 1
                names = {item.get("name", item.get("function", {}).get("name")) for item in kwargs["tools"]}
                assert "create_journal_entry" not in names
                assert "update_budget_line" not in names
                assert "send_invoice_reminder" not in names
                assert {"get_cashflow_snapshot", "update_cashflow_cells", "teach_peregrine"} <= names
            usage = N(
                prompt_tokens=10, completion_tokens=5, total_tokens=15, input_tokens=10, output_tokens=5
            )
            if self.provider == "anthropic":
                content = (
                    [N(type="tool_use", id="fixture-call", name=tool[0], input=tool[1])]
                    if tool
                    else [N(type="text", text=text)]
                )
                return N(content=content, stop_reason="tool_use" if tool else "end_turn", usage=usage)
            calls = (
                [N(id="fixture-call", function=N(name=tool[0], arguments=json.dumps(tool[1])))]
                if tool
                else []
            )
            return N(
                choices=[
                    N(
                        message=N(content=text, tool_calls=calls),
                        finish_reason="tool_calls" if calls else "stop",
                    )
                ],
                usage=usage,
            )

        def stream(self, **kwargs):
            return FakeStream(self.create(**kwargs))

    runtime.ai_client_factory = FixtureClient
    with TestClient(app) as client:
        bootstrap = client.get("/local/bootstrap").json()
        headers = {"x-csrf-token": bootstrap["csrf_token"]}
        scope = client.post("/local/demo", headers=headers).json()["workspace"]
        headers.update({"x-vesper-workspace": scope["id"], "x-vesper-revision": str(scope["revision"])})
        with runtime.session_factory() as db:
            grid = cashflow.cached_forecast(runtime, scope, db)
        original_base = cashflow.financial_fingerprint(runtime, scope)
        source_before = runtime.workspaces.read_source(scope)

        def audit_state():
            from uuid import UUID
            from app.models import (
                CashForecastActual,
                CashForecastOverride,
                CashForecastWeekStatus,
                CashForecastSnapshot,
                CashForecastSnapshotVersion,
                CashForecastExportSnapshot,
            )

            with proposals._scoped_session(runtime, scope) as db:

                def cell(value):
                    if isinstance(value, UUID):
                        return str(value)
                    return bytes(value).hex() if isinstance(value, (bytes, memoryview)) else value

                return proposals.digest(
                    {
                        model.__tablename__: [
                            {
                                column.name: cell(getattr(row, column.name))
                                for column in model.__table__.columns
                            }
                            for row in db.query(model)
                            .filter(model.company_id == scope["realm_id"])
                            .order_by(model.id)
                            .all()
                        ]
                        for model in (
                            CashForecastActual,
                            CashForecastOverride,
                            CashForecastWeekStatus,
                            CashForecastSnapshot,
                            CashForecastSnapshotVersion,
                            CashForecastExportSnapshot,
                        )
                    }
                )

        provider = "anthropic" if scenario in {"stream", "stream_errors"} else "openai"
        # A dummy non-credential travels only into an injected fake SDK client.
        saved = client.post(
            "/local/connections/model",
            headers=headers,
            json={
                "provider": provider,
                "model": "fixture-model",
                "api_key": secret_marker if scenario == "stream_errors" else "fixture-only-not-a-real-key",
                "consent": True,
                "persist": False,
            },
        )
        assert saved.status_code == 200, saved.text

        def ask(message="Explain the forecast", *, stream=False):
            response = client.post(
                "/assistant/stream" if stream else "/assistant/query",
                headers=headers,
                json={"realm_id": scope["realm_id"], "message": message},
            )
            assert response.status_code == 200, response.text
            return response

        if scenario in {"read", "numeric"}:
            from pe_platform.backend.ai import _ClientProxy

            compat = _ClientProxy(
                N(chat=N(completions=N(create=lambda **kwargs: kwargs))),
                runtime,
                scope,
                {"provider": "openai", "model": "gpt-5-fixture"},
                original_base,
            )
            sent = compat.chat.completions.create(model="gpt-5-fixture", max_tokens=32, temperature=0.2)
            assert sent["store"] is False and sent["max_completion_tokens"] == 32
            assert "max_tokens" not in sent and "temperature" not in sent
            token = proposals._NO_PROVIDER.set(True)
            try:
                try:
                    compat.chat.completions.create(model="gpt-5-fixture")
                except RuntimeError:
                    pass
                else:
                    raise AssertionError("Preview attempted a provider call")
            finally:
                proposals._NO_PROVIDER.reset(token)
            tested = client.post("/local/connections/model/test", headers=headers)
            assert tested.status_code == 200 and tested.json()["ok"]
            assert "separate check" in tested.json()["message"]
            plan[:] = [
                ("", ("get_cashflow_snapshot", {})),
                (
                    (
                        f"The forecast ending cash on {grid['week_headers'][-1]} is $987,654,321.00."
                        if scenario == "numeric"
                        else f"The forecast ending cash on {grid['week_headers'][-1]} is ${grid['ending_balances'][-1]:,.2f}."
                    ),
                    None,
                ),
            ]
            answer = ask().json()
            assert answer["actions"][0]["tool"] == "get_cashflow_snapshot"
            assert answer["actions"][0]["facts"], answer["actions"]
            assert answer["explanation"]["local_base_fingerprint"] == original_base
            assert answer["explanation"]["claims"]["numeric_claim_count"] == 1
            if scenario == "numeric":
                assert answer["explanation"]["critical_warnings"]
                assert answer["explanation"]["claims"]["unverified_claims"]
            else:
                assert answer["explanation"]["claims"]["verified_claim_count"] == 1
            assert cashflow.financial_fingerprint(runtime, scope) == original_base
            history = client.get(
                "/assistant/history", params={"realm_id": scope["realm_id"]}, headers=headers
            ).json()
            assert [item["role"] for item in history] == ["user", "assistant"]
            assert history[-1]["metadata"]["verification_schema_version"] == 1
            runs = client.get("/agent/runs", headers=headers).json()
            assert runs[0]["status"] == "succeeded" and runs[0]["model_name"] == "fixture-model"
            assert client.get(f"/agent/runs/{runs[0]['id']}", headers=headers).json()["steps"]
            assert client.get("/local/proposals", headers=headers).json()["proposals"] == []

        elif scenario == "freshness":
            from app.models import ChatMessage, ChatConversation

            old_marker = "BASE_A_ONLY_FIGURES_987654321"
            plan[:] = [("Historical " + old_marker + " is $987,654,321.00.", None)]
            ask("Discuss " + old_marker)
            history_a = client.get(
                "/assistant/history", params={"realm_id": scope["realm_id"]}, headers=headers
            ).json()
            assert len(history_a) == 2 and old_marker in str(history_a)
            assert all(row["metadata"]["local_base_fingerprint"] == original_base for row in history_a)
            with proposals._scoped_session(runtime, scope) as db:
                old_conversation = db.query(ChatConversation).filter_by(realm_id=scope["realm_id"]).one()
                old_conversation_id = old_conversation.id
                retained = [
                    (row.id, row.content, row.metadata_json)
                    for row in db.query(ChatMessage)
                    .filter_by(conversation_id=old_conversation_id)
                    .order_by(ChatMessage.id)
                ]
            changed = client.post(
                "/cashflow-forecast/override",
                headers=headers,
                json={
                    "company_id": scope["realm_id"],
                    "entity_name": grid["customers"][0]["name"],
                    "entity_type": "customer",
                    "week_ending": grid["week_headers"][2],
                    "override_amount": 8000,
                    "note": "Synthetic human change to finance base B",
                },
            )
            assert changed.status_code == 200, changed.text
            base_b = cashflow.financial_fingerprint(runtime, scope)
            assert base_b != original_base
            assert (
                client.get(
                    "/assistant/history", params={"realm_id": scope["realm_id"]}, headers=headers
                ).json()
                == []
            )
            offset = len(physical_calls)
            plan[:] = [("The current forecast requires current source evidence.", None)]
            ask("Explain the current forecast after my change")
            assert physical_calls[offset:] and old_marker not in str(physical_calls[offset:])
            history_b = client.get(
                "/assistant/history", params={"realm_id": scope["realm_id"]}, headers=headers
            ).json()
            assert len(history_b) == 2 and old_marker not in str(history_b)
            assert all(row["metadata"]["local_base_fingerprint"] == base_b for row in history_b)
            with proposals._scoped_session(runtime, scope) as db:
                assert db.query(ChatConversation).filter_by(realm_id=scope["realm_id"]).count() == 2
                assert retained == [
                    (row.id, row.content, row.metadata_json)
                    for row in db.query(ChatMessage)
                    .filter_by(conversation_id=old_conversation_id)
                    .order_by(ChatMessage.id)
                ]
        elif scenario in {"proposal", "memory", "mismatch"}:
            if scenario == "memory":
                tool = (
                    "teach_peregrine",
                    {
                        "category": "General",
                        "fact": "Synthetic customer collection dates require explicit review.",
                    },
                )
            else:
                tool = (
                    "update_cashflow_cells",
                    {
                        "updates": [
                            {
                                "entity_name": grid["customers"][0]["name"],
                                "entity_type": "customer",
                                "week_ending": grid["week_headers"][2],
                                "amount": 8000,
                                "note": "Synthetic assistant proposal, explicitly reviewed",
                            }
                        ]
                    },
                )
            plan[:] = [
                ("", tool),
                ("A proposal is pending human review. The forecast has not changed.", None),
            ]
            ask("Draft a synthetic change")
            assert cashflow.financial_fingerprint(runtime, scope) == original_base
            listing = client.get("/local/proposals", headers=headers).json()["proposals"]
            assert len(listing) == 1 and listing[0]["status"] == "PENDING"
            proposal = listing[0]
            url = f"/local/proposals/{proposal['id']}"
            with proposals._scoped_session(runtime, scope) as db:
                from app.models import ProposedAction

                row = db.get(ProposedAction, proposal["id"])
                assert row.human_preparer_user_id is None and row.last_human_editor_user_id is None
            assert (
                client.post(
                    url + "/approve",
                    headers=headers,
                    json={"review_fingerprint": proposal["review_fingerprint"]},
                ).status_code
                == 409
            )
            original_audit_state = audit_state()
            preview = client.post(
                url + "/preview", headers=headers, json={"review_fingerprint": proposal["review_fingerprint"]}
            )
            assert preview.status_code == 200 and preview.json()["available"], preview.text
            preview = preview.json()
            assert cashflow.financial_fingerprint(runtime, scope) == original_base
            assert runtime.workspaces.read_source(scope) == source_before
            assert audit_state() == original_audit_state
            assert preview["before"]["week_headers"] == preview["after"]["week_headers"]
            assert preview["comparison"]["ending_cash_delta"] == (0 if scenario == "memory" else -4000)
            invalid = {"review_fingerprint": preview["review_fingerprint"], "preview_token": "wrong"}
            assert client.post(url + "/approve", headers=headers, json=invalid).status_code == 409
            if scenario == "mismatch":
                from sqlalchemy import event
                from sqlalchemy.orm import Session
                from app.models import CashForecastOverride

                def diverge_live_only(db, _flush_context, _instances):
                    if db.get_bind() is runtime.engine and proposals._EXECUTION_REVIEW.get() is not None:
                        for row in list(db.new) + list(db.dirty):
                            if isinstance(row, CashForecastOverride) and row.override_amount == 8000:
                                row.override_amount = 9000

                event.listen(Session, "before_flush", diverge_live_only)
                try:
                    rejected = client.post(
                        url + "/approve",
                        headers=headers,
                        json={
                            "review_fingerprint": preview["review_fingerprint"],
                            "preview_token": preview["preview_token"],
                        },
                    )
                finally:
                    event.remove(Session, "before_flush", diverge_live_only)
                assert rejected.status_code == 200 and not rejected.json()["ok"], rejected.text
                assert rejected.json()["outcome"] == "recovery_required", rejected.text
                assert (
                    rejected.json()["proposal"]["approval_snapshots"]["financial_rollback_confirmed"] is True
                )
                assert cashflow.financial_fingerprint(runtime, scope) == original_base
                with proposals._scoped_session(runtime, scope) as db:
                    assert db.query(CashForecastOverride).filter_by(company_id=scope["realm_id"]).count() == 0
                    from app.models import InternalActionExecution

                    ledger = db.query(InternalActionExecution).filter_by(company_id=scope["realm_id"]).one()
                    assert ledger.status == "recovery_required"
                assert (
                    client.post(
                        url + "/approve",
                        headers=headers,
                        json={
                            "review_fingerprint": preview["review_fingerprint"],
                            "preview_token": preview["preview_token"],
                        },
                    ).status_code
                    == 409
                )
                return
            approval = client.post(
                url + "/approve",
                headers=headers,
                json={
                    "review_fingerprint": preview["review_fingerprint"],
                    "preview_token": preview["preview_token"],
                },
            )
            assert approval.status_code == 200 and approval.json()["ok"], approval.text
            result = approval.json()["proposal"]
            assert result["status"] == "EXECUTED" and result["reviewed_by"]
            snapshots = result["approval_snapshots"]
            assert snapshots["before_export"]["export_id"] and snapshots["after_export"]["export_id"]
            assert snapshots["before_export"]["content_hash"] and snapshots["after_export"]["content_hash"]
            assert (
                client.post(
                    url + "/approve",
                    headers=headers,
                    json={
                        "review_fingerprint": preview["review_fingerprint"],
                        "preview_token": preview["preview_token"],
                    },
                ).status_code
                == 409
            )
            if scenario == "memory":
                with proposals._scoped_session(runtime, scope) as db:
                    from app.models import CompanyKnowledge

                    memory = db.query(CompanyKnowledge).filter_by(company_id=scope["realm_id"]).all()
                    assert len(memory) == 1 and memory[0].accepted_by
                plan[:] = [
                    ("", ("get_module_context", {"module": "knowledge"})),
                    ("The accepted synthetic rule is available as evidence.", None),
                ]
                recalled = ask("Retrieve accepted company knowledge").json()
                assert "Synthetic customer collection dates" in recalled["actions"][0]["result"], recalled
            else:
                with runtime.session_factory() as db:
                    after = cashflow.cached_forecast(runtime, scope, db)
                assert after["ending_balances"] == preview["after"]["ending_balances"]
                assert cashflow.financial_fingerprint(runtime, scope) != original_base
                assert (
                    client.get(
                        "/assistant/history", params={"realm_id": scope["realm_id"]}, headers=headers
                    ).json()
                    == []
                )
            # Draft against current state, then alter a native financial fact.
            plan[:] = [("", tool), ("A second proposal needs review.", None)]
            ask("Draft again")
            pending = [
                row
                for row in client.get("/local/proposals", headers=headers).json()["proposals"]
                if row["status"] == "PENDING"
            ][0]
            pending_preview = client.post(
                f"/local/proposals/{pending['id']}/preview",
                headers=headers,
                json={"review_fingerprint": pending["review_fingerprint"]},
            ).json()
            assert pending_preview["available"]
            with proposals._scoped_session(runtime, scope) as db:
                from app.models import CashForecastOverride
                from datetime import date

                db.add(
                    CashForecastOverride(
                        company_id=scope["realm_id"],
                        entity_name="Synthetic stale guard",
                        entity_type="vendor",
                        week_ending=date.fromisoformat(grid["week_headers"][3]),
                        override_amount=123,
                        note="Synthetic separate human action",
                        created_by="local_operator",
                    )
                )
                db.commit()
            stale_url = f"/local/proposals/{pending['id']}"
            assert (
                client.post(
                    stale_url + "/approve",
                    headers=headers,
                    json={
                        "review_fingerprint": pending_preview["review_fingerprint"],
                        "preview_token": pending_preview["preview_token"],
                    },
                ).status_code
                == 409
            )
            assert (
                client.post(
                    stale_url + "/preview",
                    headers=headers,
                    json={"review_fingerprint": pending["review_fingerprint"]},
                ).status_code
                == 409
            )
            rejected = client.post(
                stale_url + "/reject",
                headers=headers,
                json={"review_fingerprint": pending["review_fingerprint"]},
            )
            assert rejected.status_code == 200 and rejected.json()["proposal"]["status"] == "REJECTED"

        elif scenario == "stream":
            plan[:] = [
                ("", ("get_cashflow_snapshot", {})),
                ("The synthetic current forecast is available for review.", None),
            ]
            response = ask(stream=True)
            events = [
                json.loads(line.removeprefix("data: "))
                for line in response.text.splitlines()
                if line.startswith("data: ")
            ]
            assert {"text", "status", "explanation", "done"} <= {event["type"] for event in events}, events
            assert not [event for event in events if event["type"] == "error"], events
            explanation = next(event["content"] for event in events if event["type"] == "explanation")
            assert explanation["local_base_fingerprint"] == original_base
            runs = client.get("/agent/runs", headers=headers).json()
            assert runs[0]["status"] == "succeeded", runs
            assert runs[0]["token_usage"]["input_tokens"] > 0
            assert (
                len(
                    client.get(
                        "/assistant/history", headers=headers, params={"realm_id": scope["realm_id"]}
                    ).json()
                )
                == 2
            )

        elif scenario == "stream_errors":
            for failure in ("enter", "iteration", "final", "exit"):
                stream_failure[:] = [failure]
                plan[:] = [("A partial synthetic response.", None)]
                response = ask(stream=True)
                assert secret_marker not in response.text
                runs = client.get("/agent/runs", headers=headers).json()
                assert runs[0]["status"] == "failed", runs
                assert secret_marker not in str(runs)
                detail = client.get(f"/agent/runs/{runs[0]['id']}", headers=headers).json()
                assert secret_marker not in str(detail)
                history = client.get(
                    "/assistant/history", headers=headers, params={"realm_id": scope["realm_id"]}
                ).json()
                assert secret_marker not in str(history)
            assert len(clients_closed) == 4 and len(streams_closed) == 4
        elif scenario == "scope":
            # Provider input cannot unlock an unrelated write, change company,
            # smuggle hidden fields or persist non-finite/partial cents.
            for name, arguments in [
                ("create_journal_entry", {}),
                ("update_cashflow_cells", {"updates": [], "company_id": "other"}),
                (
                    "update_cashflow_cells",
                    {
                        "updates": [
                            {
                                "entity_name": "Synthetic",
                                "entity_type": "vendor",
                                "week_ending": grid["week_headers"][2],
                                "amount": 0.001,
                            }
                        ]
                    },
                ),
            ]:
                plan[:] = [("", (name, arguments)), ("The attempted action was not authorised.", None)]
                response = ask("Inspect policy").json()
                assert "blocked" in response["actions"][0]["result"].lower(), response
            assert client.get("/local/proposals", headers=headers).json()["proposals"] == []
            assert cashflow.financial_fingerprint(runtime, scope) == original_base
            old_headers = dict(headers)
            other = client.post(
                "/local/workspaces", headers=headers, json={"name": "Synthetic second entity"}
            ).json()["workspace"]
            headers.update({"x-vesper-workspace": other["id"], "x-vesper-revision": str(other["revision"])})
            assert (
                client.get(
                    "/assistant/history", headers=headers, params={"realm_id": other["realm_id"]}
                ).json()
                == []
            )
            assert (
                client.get(
                    "/assistant/history", headers=headers, params={"realm_id": scope["realm_id"]}
                ).status_code
                == 403
            )
            assert client.get("/local/proposals", headers=old_headers).status_code == 409
            before_calls = len(physical_calls)
            refused = client.post(
                "/assistant/query",
                headers=headers,
                json={"realm_id": other["realm_id"], "message": "Explain"},
            )
            assert refused.status_code == 409 and len(physical_calls) == before_calls
        elif scenario == "catalog":
            for name, arguments in [
                ("get_cashflow_snapshot", {}),
                ("analyze_cashflow_position", {"focus": "liquidity"}),
                ("get_cashflow_actuals_review", {}),
                ("get_weekly_review", {}),
                ("get_module_context", {"module": "cashflow"}),
                ("get_module_context", {"module": "knowledge"}),
                ("get_module_context", {"module": "corrections"}),
                ("refresh_ar_ap_snapshot", {}),
                ("get_ar_ap_summary", {}),
                ("refresh_cashflow_forecast", {}),
            ]:
                plan[:] = [
                    ("", (name, arguments)),
                    ("The requested synthetic cash-flow evidence was inspected.", None),
                ]
                response = ask("Inspect " + name).json()
                outcome = response["actions"][0].get("outcome", {})
                if name == "get_module_context" and arguments["module"] in {"knowledge", "corrections"}:
                    assert (
                        outcome.get("status") == "succeeded"
                        or outcome.get("code") == "module_context_unavailable"
                    ), response["actions"]
                else:
                    assert outcome.get("status") == "succeeded", response["actions"]
                assert not response["actions"][0]["result"].startswith("Error"), response["actions"]
            # Exercise the other native cash proposal executors/preview surfaces.
            from datetime import date
            from app.models import CashForecastActual, CashForecastOverride

            with proposals._scoped_session(runtime, scope) as db:
                actual = CashForecastActual(
                    company_id=scope["realm_id"],
                    qbo_txn_id="fixture-categorize",
                    qbo_txn_type="Payment",
                    txn_date=date.fromisoformat(grid["week_headers"][0]),
                    amount=25,
                    direction="in",
                    week_ending=date.fromisoformat(grid["week_headers"][0]),
                    category="uncategorized",
                )
                override = CashForecastOverride(
                    company_id=scope["realm_id"],
                    entity_name="Synthetic delete target",
                    entity_type="vendor",
                    week_ending=date.fromisoformat(grid["week_headers"][3]),
                    override_amount=25,
                    created_by="local_operator",
                )
                db.add_all([actual, override])
                db.commit()
                actual_id, override_id = actual.id, override.id
            for name, arguments in [
                (
                    "layer_cashflow_schedule",
                    {
                        "entity_name": "Synthetic manual schedule",
                        "entity_type": "vendor",
                        "day_of_month": 15,
                        "avg_amount": 100,
                        "weekly_amounts": [{"week_ending": grid["week_headers"][3], "amount": 75}],
                    },
                ),
                (
                    "spread_cashflow_balance",
                    {
                        "entity_name": "Synthetic native spread",
                        "entity_type": "vendor",
                        "total_amount": 300,
                        "periods": 3,
                        "cadence": "weekly",
                        "start_week_ending": grid["week_headers"][2],
                    },
                ),
                (
                    "categorize_cashflow_actuals",
                    {
                        "items": [
                            {
                                "id": actual_id,
                                "entity_name": "Other Cash In",
                                "entity_type": "other_cash_in",
                                "category": "categorized",
                            }
                        ]
                    },
                ),
                ("delete_cashflow_override", {"override_id": override_id}),
            ]:
                prior = cashflow.financial_fingerprint(runtime, scope)
                original_audit_state = audit_state()
                original_source = runtime.workspaces.read_source(scope)
                plan[:] = [("", (name, arguments)), ("The exact cash-flow proposal requires review.", None)]
                response = ask("Draft " + name).json()
                assert (response["actions"][0].get("outcome", {}).get("action") or {}).get(
                    "outcome"
                ) == "drafted", response
                proposal = client.get("/local/proposals", headers=headers).json()["proposals"][0]
                url = f"/local/proposals/{proposal['id']}"
                preview = client.post(
                    url + "/preview",
                    headers=headers,
                    json={"review_fingerprint": proposal["review_fingerprint"]},
                )
                assert preview.status_code == 200 and preview.json()["available"], (name, preview.text)
                assert cashflow.financial_fingerprint(runtime, scope) == prior
                assert runtime.workspaces.read_source(scope) == original_source
                assert audit_state() == original_audit_state
                preview = preview.json()
                approved = client.post(
                    url + "/approve",
                    headers=headers,
                    json={
                        "review_fingerprint": preview["review_fingerprint"],
                        "preview_token": preview["preview_token"],
                    },
                )
                assert approved.status_code == 200 and approved.json()["ok"], (name, approved.text)
                with runtime.session_factory() as db:
                    after = cashflow.cached_forecast(runtime, scope, db)
                assert after["ending_balances"] == preview["after"]["ending_balances"]
        assert "fixture-only-not-a-real-key" not in str(client.get("/agent/runs", headers=headers).json())


if __name__ == "__main__":
    # Running this file directly keeps all native imports in this isolated child.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    _exercise(sys.argv[1])

"""Refresh economic controls using an isolated, synthetic accounting acquisition."""

from copy import deepcopy
from datetime import datetime, timedelta
import json

from fastapi.testclient import TestClient

from vesper_cashflow.models import COVERAGE_SECTIONS, digest
from vesper_cashflow.web import create_app


def test_refresh_keeps_reviewed_assumptions_but_requires_fresh_coverage(tmp_path):
    today = datetime.now().astimezone().date()
    tomorrow = (today + timedelta(days=1)).isoformat()
    yesterday = (today - timedelta(days=1)).isoformat()
    manual_source = {
        "name": "Synthetic dated assumptions",
        "sha256": digest("synthetic manual evidence"),
        "kind": "csv",
        "details": "Explicit tax and full amount per payroll occurrence",
    }
    raw = {
        "as_of": (today - timedelta(days=2)).isoformat(),
        "currency": "USD",
        "opening_cash": "1000.00",
        "opening_source": "Synthetic earlier cash anchor",
        "items": [
            {
                "id": "invoice-1", "name": "Synthetic invoice", "category": "ar",
                "direction": "in", "amount": "250.00", "due_date": yesterday,
                "expected_date": tomorrow, "timing_basis": "Reviewed collection commitment",
                "source": "Earlier accounting acquisition",
            },
            {
                "id": "manual-tax", "name": "Synthetic tax", "category": "assumption",
                "direction": "out", "amount": "50.00", "due_date": yesterday,
                "expected_date": yesterday, "timing_basis": "Reviewed tax payment schedule",
                "source": "Synthetic dated assumptions", "source_id": "tax-row", "ordinal": 8,
            },
        ],
        "recurrences": [{
            "id": "payroll", "name": "Synthetic payroll", "direction": "out",
            "amount": "100.00", "cadence": "biweekly", "anchor_date": tomorrow,
            "start_date": tomorrow, "end_date": (today + timedelta(days=90)).isoformat(),
            "timing_basis": "Full $100 each payday", "source": "Synthetic dated assumptions",
        }],
        "coverage": {section: "complete" for section in COVERAGE_SECTIONS},
        "sources": [manual_source],
    }
    app = create_app(tmp_path, port=8766, testing=True)
    with TestClient(app, base_url="http://127.0.0.1:8766") as client:
        client.get("/")
        csrf = client.get("/api/bootstrap").json()["csrf_token"]
        client.headers.update({"Origin": "http://127.0.0.1:8766", "X-Vesper-CSRF": csrf})
        app.state.store.create_workspace("Synthetic refresh-control entity")
        before = app.state.store.save_version(raw, "Reviewed before refresh")
        snapshot = deepcopy(before)

        class AccountingFixture:
            def _require_bound(self):
                return None

            def forecast_inputs(self, as_of):
                return {
                    "opening_cash": "2000.00", "currency": "USD",
                    "items": [
                        {"id": "invoice-1", "name": "Synthetic invoice updated balance",
                         "direction": "in", "amount": "400.00", "due_date": as_of,
                         "source": "Fresh synthetic invoice balance"},
                        {"id": "bill-1", "name": "Synthetic unreviewed bill",
                         "direction": "out", "amount": "25.00", "due_date": tomorrow,
                         "source": "Fresh synthetic bill balance"},
                    ],
                    "coverage": {key: {"complete": True} for key in ("cash", "accounts", "invoice", "bill")},
                    "source": {"complete": True, "as_of": as_of},
                }

        app.state.common.qbo_factory = lambda workspace_id: AccountingFixture()
        for _ in range(2):
            scope = client.get("/api/state").json()["scope"]
            response = client.post(
                "/api/qbo/sync", json={"as_of": today.isoformat()},
                headers={"X-Vesper-Scope": json.dumps(scope)},
            )
            assert response.status_code == 200, response.text
            current = app.state.store.get_version()
            inputs, result = current["inputs"], current["result"]
            assert {row["id"] for row in inputs["items"]} == {"invoice-1", "bill-1", "manual-tax"}
            assert len(inputs["items"]) == 3
            invoice = next(row for row in inputs["items"] if row["id"] == "invoice-1")
            assert (invoice["amount"], invoice["due_date"]) == ("400.00", today.isoformat())
            assert (invoice["expected_date"], invoice["timing_basis"]) == (tomorrow, "Reviewed collection commitment")
            tax = next(row for row in inputs["items"] if row["id"] == "manual-tax")
            assert tax == snapshot["inputs"]["items"][1]
            assert inputs["recurrences"] == snapshot["inputs"]["recurrences"]
            assert snapshot["inputs"]["sources"][0] in inputs["sources"]
            assert inputs["coverage"] == {
                section: "complete" if section in {"opening_cash", "ar", "ap"} else "missing"
                for section in COVERAGE_SECTIONS
            }
            assert len(result["occurrences"]) == 7
            assert {row["amount"] for row in result["occurrences"]} == {"100.00"}
            assert result["total_in"] == "400.00"
            assert result["total_out"] == "700.00"
            assert result["ending_cash"] == "1700.00"
            assert result["coverage_complete"] is False
            assert any(row["code"] == "past_expected" and row["ids"] == ["manual-tax"] for row in result["findings"])
            assert any(row["code"] == "undated" and row["ids"] == ["bill-1"] for row in result["findings"])
        assert app.state.store.get_version(before["id"]) == snapshot

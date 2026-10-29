"""GL population and identity checks use synthetic reports, never live credentials."""

from __future__ import annotations

import copy
from datetime import date, timedelta
import json
from pathlib import Path
import re
import subprocess
import sys
import threading
from types import SimpleNamespace

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import pytest

from pe_platform.backend import drivers
from pe_platform.backend.connections import NativeReadOnlyClient, RecordedClient, client_for_scope
from vesper_cashflow.connectors.qbo import QBOError

TODAY = date(2026, 10, 29)
START = TODAY - timedelta(days=180)


def contact(identity, name, active=True):
    return {"Id": identity, "DisplayName": name, "Active": active}


def line(name="Alder", identity="c1", day=None, kind="Payment"):
    return {"type": "Data", "ColData": [
        {"value": (day or TODAY).isoformat()}, {"value": name, "id": identity}, {"value": kind},
    ]}


def report(rows=None, **header):
    return {
        "Header": {"ReportName": "GeneralLedger", "Currency": "USD", "ReportBasis": "Accrual",
                   "StartPeriod": START.isoformat(), "EndPeriod": TODAY.isoformat(), **header},
        "Columns": {"Column": [
            {"ColTitle": "Date", "ColType": "Date", "MetaData": [{"Name": "ColKey", "Value": "tx_date"}]},
            {"ColTitle": "Name", "ColType": "Name", "MetaData": [{"Name": "ColKey", "Value": "name"}]},
            {"ColTitle": "Transaction Type", "ColType": "String"},
        ]},
        "Rows": {"Row": [line()] if rows is None else rows},
    }


class Client:
    def __init__(self, gl=None, customers=None, vendors=None, failed_contacts=None):
        self.gl = report() if gl is None else gl
        self.customers = [contact("c1", "Alder")] if customers is None else customers
        self.vendors = [] if vendors is None else vendors
        self.failed_contacts = failed_contacts
        self.calls = []

    def get_report(self, name, params):
        self.calls.append((name, params))
        if isinstance(self.gl, Exception):
            raise self.gl
        return copy.deepcopy(self.gl)

    def query(self, query):
        self.calls.append(query)
        assert query.startswith("SELECT * FROM ")
        entity = re.search(r"FROM\s+(Customer|Vendor)", query)[1]
        assert "WHERE Active IN (true, false)" in query
        if self.failed_contacts == entity:
            raise ValueError("secret-provider-marker")
        start = int(re.search(r"STARTPOSITION (\d+)", query)[1])
        size = int(re.search(r"MAXRESULTS (\d+)", query)[1])
        source = self.customers if entity == "Customer" else self.vendors
        rows = source[start - 1:start - 1 + size]
        return {"QueryResponse": {entity: copy.deepcopy(rows), "startPosition": start, "maxResults": len(rows)}}


def fetch(client):
    return drivers.fetch_catalog(client, company_id="realm-a", today=TODAY)


def test_gl_supplies_population_including_inactive_not_unused_master_names():
    client = Client(
        report([line(), line("Old legal", "v1", kind="Deposit"), line("Beacon", "c2", kind="Expense")]),
        customers=[contact("c1", "Alder"), contact("c2", "Beacon", False), contact("c9", "Unused customer")],
        vendors=[contact("v1", "Old legal", False), contact("v9", "Unused vendor")],
    )
    result = fetch(client)
    assert result["coverage"]["status"] == "complete"
    assert {(entry["name"], entry["entity_type"], entry["active"]) for entry in result["entries"]} == {
        ("Alder", "customer", True), ("Beacon", "customer", False), ("Old legal", "vendor", False),
    }
    assert client.calls[0] == ("GeneralLedger", {
        "start_date": START.isoformat(), "end_date": TODAY.isoformat(), "accounting_method": "Accrual",
    })
    assert result["coverage"]["named_rows"] == result["coverage"]["data_rows"] == 3


def test_nested_sections_ignore_headers_summaries_opening_balances_and_unattributed_lines():
    opening = line("Not a real contact", "phantom")
    opening["ColData"][0] = {"value": "Beginning Balance"}
    source = report([{
        "type": "Section", "Header": {"ColData": [{"value": "Header contact", "id": "phantom"}]},
        "Rows": {"Row": [{"type": "Section", "Rows": {"Row": [opening, line(), line("", "")]}}]},
        "Summary": {"ColData": [{"value": TODAY.isoformat()}, {"value": "Summary contact"}]},
    }])
    result = fetch(Client(source))
    assert result["coverage"] == {"status": "complete", "issues": [], "data_rows": 2, "named_rows": 1, "unattributed_rows": 1}
    assert [entry["name"] for entry in result["entries"]] == ["Alder"]


def test_aliases_and_duplicate_display_names_preserve_observed_identity_and_stable_order():
    source = report([line("Old Alder", "c1", START), line("Alder", "c1"), line("Alder", "c2")])
    client = Client(source, customers=[contact("c1", "Alder"), contact("c2", "Alder")])
    first = fetch(client)
    assert len(first["entries"]) == 2
    by_id = {entry["entity_id"]: entry for entry in first["entries"]}
    assert by_id["c1"]["aliases"] == ["Old Alder"]
    assert by_id["c1"]["first_seen"] == START.isoformat()
    assert by_id["c1"]["last_seen"] == TODAY.isoformat()
    assert by_id["c1"]["gl_line_count"] == 2
    shuffled = copy.deepcopy(source)
    shuffled["Rows"]["Row"].reverse()
    # Column identity follows metadata, not a hard-coded position or visible title.
    shuffled["Columns"]["Column"] = list(reversed(shuffled["Columns"]["Column"]))
    for row in shuffled["Rows"]["Row"]:
        row["ColData"].reverse()
    second = fetch(Client(shuffled, customers=list(reversed(client.customers))))
    assert second["entries"] == first["entries"]
    assert len({entry["key"] for entry in first["entries"]}) == 2


@pytest.mark.parametrize("identity, customers, vendors", [
    ("missing", [contact("c1", "Alder")], []),
    ("c1", [contact("c1", "Alder")], [contact("c1", "Vendor with same ID")]),
])
def test_present_unresolved_gl_ids_are_never_rescued_by_matching_names(identity, customers, vendors):
    result = fetch(Client(report([line(identity=identity)]), customers=customers, vendors=vendors))
    assert result["coverage"]["status"] == "partial"
    entry = result["entries"][0]
    assert entry["entity_type"] == entry["identity_basis"] == "unresolved"
    assert entry["entity_id"] == identity


def test_ambiguous_untyped_ids_do_not_merge_distinct_observed_names():
    result = fetch(Client(report([line("Alder", "same"), line("Other", "same")]),
                          customers=[contact("same", "Alder")], vendors=[contact("same", "Other")]))
    assert len(result["entries"]) == 2
    assert {entry["name"] for entry in result["entries"]} == {"Alder", "Other"}
    assert all(entry["identity_basis"] == "unresolved" for entry in result["entries"])


def test_name_only_fallback_does_not_invent_a_gl_observed_id_and_ambiguity_remains_unresolved():
    result = fetch(Client(report([line(identity=None)])))
    entry = result["entries"][0]
    assert entry["entity_type"] == "customer" and entry["entity_id"] is None
    assert entry["identity_basis"] == "unique_name" and result["coverage"]["status"] == "partial"
    ambiguous = fetch(Client(report([line(identity=None)]), vendors=[contact("v1", "Alder")]))
    assert ambiguous["entries"][0]["identity_basis"] == "unresolved"


def test_invalid_present_id_is_not_treated_as_absent_identity():
    result = fetch(Client(report([line(identity={"secret": "marker"})])))
    assert result["entries"][0]["identity_basis"] == "unresolved"
    assert "marker" not in json.dumps(result)


def test_partial_contact_side_cannot_make_generic_names_authoritative():
    result = fetch(Client(failed_contacts="Vendor"))
    assert result["entries"][0]["entity_type"] == "unresolved"
    assert result["coverage"]["status"] == "partial"
    assert "secret-provider-marker" not in json.dumps(result)
    assert "contacts_unavailable" in {issue["code"] for issue in result["coverage"]["issues"]}


def typed_report():
    result = report([])
    result["Columns"]["Column"] = [
        {"ColTitle": "Date", "ColType": "Date"},
        {"ColTitle": "Customer", "ColType": "Name", "MetaData": [{"Name": "ColKey", "Value": "cust_name"}]},
        {"ColTitle": "Vendor", "ColType": "Name", "MetaData": [{"Name": "ColKey", "Value": "vend_name"}]},
        {"ColTitle": "Name", "ColType": "Name"},
    ]
    result["Rows"]["Row"] = [{"type": "Data", "ColData": [
        {"value": TODAY.isoformat()}, {"value": "Both roles", "id": "same"},
        {"value": "Both roles", "id": "same"}, {"value": "Both roles", "id": "same"},
    ]}]
    return result


def test_explicit_typed_gl_columns_preserve_both_parties_with_type_scoped_ids():
    result = fetch(Client(typed_report(), customers=[contact("same", "Both roles")], vendors=[contact("same", "Both roles")]))
    assert result["coverage"]["status"] == "complete"
    assert {(entry["entity_type"], entry["entity_id"], entry["identity_basis"]) for entry in result["entries"]} == {
        ("customer", "same", "typed_gl"), ("vendor", "same", "typed_gl"),
    }
    assert all(entry["gl_line_count"] == 1 for entry in result["entries"])
    assert result["coverage"]["named_rows"] == 1
    partial = fetch(Client(typed_report(), failed_contacts="Vendor"))
    assert {entry["entity_type"] for entry in partial["entries"]} == {"customer", "vendor"}
    assert all(entry["active"] is None for entry in partial["entries"])


def test_typed_gl_identity_conflicts_and_missing_ids_require_review_without_reclassification():
    source = typed_report()
    source["Rows"]["Row"][0]["ColData"][2] = {"value": ""}
    source["Rows"]["Row"][0]["ColData"][3] = {"value": ""}
    conflict = fetch(Client(source, customers=[], vendors=[contact("same", "Both roles")]))
    assert conflict["coverage"]["status"] == "partial"
    assert conflict["entries"][0]["entity_type"] == "customer"
    assert conflict["entries"][0]["entity_id"] == "same"
    assert conflict["entries"][0]["identity_basis"] == "unresolved"
    source["Rows"]["Row"][0]["ColData"][1].pop("id")
    no_id = fetch(Client(source, customers=[contact("c1", "Both roles")]))
    assert no_id["coverage"]["status"] == "partial"
    assert no_id["entries"][0]["entity_id"] is None
    assert no_id["entries"][0]["identity_basis"] == "unique_name"


@pytest.mark.parametrize("value", ["name", "cust_name"])
def test_duplicate_or_conflicting_colkey_metadata_is_not_rescued_by_visible_titles(value):
    source = report()
    source["Columns"]["Column"][1]["MetaData"].append({"Name": "ColKey", "Value": value})
    result = fetch(Client(source))
    assert result["coverage"]["status"] == "unavailable"
    assert result["coverage"]["issues"][0]["code"] == "gl_columns_invalid"


def test_nested_rows_truncation_flag_is_not_hidden_by_a_complete_outer_report():
    source = report([{"type": "Section", "Rows": {"Row": [line()], "has_more_data": True}}])
    result = fetch(Client(source))
    assert result["coverage"]["status"] == "partial"
    assert result["coverage"]["issues"][0]["code"] == "gl_truncated"


def test_transaction_data_cannot_hide_nested_unread_gl_rows():
    source = report()
    source["Rows"]["Row"][0]["Rows"] = {"Row": [line("Hidden", "c2")]}
    result = fetch(Client(source))
    assert result["coverage"]["status"] == "partial" and result["entries"] == []
    assert result["coverage"]["issues"][0]["code"] == "gl_rows_invalid"


@pytest.mark.parametrize("flag", ["fault", "truncated", "count"])
def test_contact_errors_or_inconsistent_paging_cannot_look_complete(flag):
    class BadContacts(Client):
        def query(self, query):
            value = super().query(query)
            if "FROM Customer" in query:
                if flag == "fault":
                    value["Fault"] = {"Error": "do-not-echo"}
                elif flag == "truncated":
                    value["has_more"] = True
                else:
                    value["QueryResponse"]["maxResults"] = 1.0
            return value

    result = fetch(BadContacts())
    assert result["coverage"]["status"] == "partial"
    assert result["entries"][0]["identity_basis"] == "unresolved"
    assert "do-not-echo" not in json.dumps(result)


@pytest.mark.parametrize("status", [403, 409])
def test_context_error_status_is_preserved_without_provider_error_prose(status):
    with pytest.raises(HTTPException) as captured:
        fetch(Client(HTTPException(status, "credential-marker-never-echo")))
    assert captured.value.status_code == status
    assert "credential-marker-never-echo" not in str(captured.value.detail)


@pytest.mark.parametrize("field,value", [
    ("ReportName", "BalanceSheet"), ("Currency", "GBP"), ("StartPeriod", "2026-01-01"),
    ("EndPeriod", "2026-10-30"), ("ReportBasis", "Cash"), ("ReportBasis", None),
    ("Option", {"Name": "Truncated", "Value": True}),
])
def test_wrong_report_identity_window_or_shape_is_unavailable(field, value):
    result = fetch(Client(report(**{field: value})))
    assert result["coverage"]["status"] == "unavailable" and result["entries"] == []


@pytest.mark.parametrize("change", ["missing_name", "duplicate_name", "missing_date", "missing_rows", "bad_rows"])
def test_malformed_gl_cannot_be_a_complete_empty_directory(change):
    source = report()
    if change == "missing_name":
        source["Columns"]["Column"][1] = {"ColTitle": "Memo", "ColType": "String"}
    elif change == "duplicate_name":
        source["Columns"]["Column"].append(source["Columns"]["Column"][1])
    elif change == "missing_date":
        source["Columns"]["Column"][0] = {"ColTitle": "Account", "ColType": "Account"}
    elif change == "missing_rows":
        source.pop("Rows")
    else:
        source["Rows"] = {"Row": {"type": "Data"}}
    result = fetch(Client(source))
    assert result["coverage"]["status"] == "unavailable"


def test_valid_empty_and_no_report_data_are_distinct_from_failures():
    for source in [report([]), report([], Option=[{"Name": "NoReportData", "Value": "true"}])]:
        client = Client(source)
        result = fetch(client)
        assert result["entries"] == [] and result["coverage"]["status"] == "complete"
        assert len(client.calls) == 1  # No unnecessary metadata read for an empty GL.
    contradictory = fetch(Client(report(Option=[{"Name": "NoReportData", "Value": "true"}])))
    assert contradictory["coverage"]["status"] == "unavailable"
    missing = report(Option=[{"Name": "NoReportData", "Value": "true"}])
    missing.pop("Rows")
    assert fetch(Client(missing))["coverage"]["status"] == "complete"
    failed = fetch(Client(ValueError("sensitive-token-never-return")))
    assert failed["coverage"]["status"] == "unavailable"
    assert "sensitive-token-never-return" not in json.dumps(failed)


def test_date_edges_are_inclusive_and_outside_or_malformed_rows_are_partial():
    source = report([line(day=START), line(day=TODAY), line(day=START - timedelta(days=1)), line(day=TODAY + timedelta(days=1))])
    broken = line()
    broken["ColData"][0]["value"] = "not-a-date"
    source["Rows"]["Row"].append(broken)
    source["Rows"]["Row"].append({"type": "Data", "ColData": [{"value": TODAY.isoformat()}]})
    result = fetch(Client(source))
    assert result["coverage"]["status"] == "partial"
    assert result["entries"][0]["gl_line_count"] == 2
    assert result["entries"][0]["first_seen"] == START.isoformat()
    assert {issue["code"] for issue in result["coverage"]["issues"]} >= {"gl_window_mismatch", "gl_rows_invalid"}


def test_explicit_truncation_row_and_contact_limits_never_establish_complete_coverage(monkeypatch):
    source = report()
    source["Header"]["Option"] = [{"Name": "is_truncated", "Value": "true"}]
    assert fetch(Client(source))["coverage"]["status"] == "partial"
    monkeypatch.setattr(drivers, "MAX_GL_ROWS", 1)
    result = fetch(Client(report([line(), line("Beacon", "c2")]), customers=[contact("c1", "Alder"), contact("c2", "Beacon")]))
    assert result["coverage"]["status"] == "partial" and len(result["entries"]) == 1
    monkeypatch.setattr(drivers, "CONTACT_PAGE_SIZE", 1)
    monkeypatch.setattr(drivers, "MAX_CONTACT_PAGES", 1)
    limited = fetch(Client())
    assert limited["coverage"]["status"] == "partial" and limited["entries"][0]["entity_type"] == "unresolved"


def test_contact_pagination_includes_inactive_rows_and_detects_non_progress(monkeypatch):
    monkeypatch.setattr(drivers, "CONTACT_PAGE_SIZE", 1)
    client = Client(customers=[contact("c1", "Alder"), contact("c2", "Dormant", False)])
    assert fetch(client)["coverage"]["status"] == "complete"
    assert any("FROM Customer" in query and "STARTPOSITION 3 " in query for query in client.calls if isinstance(query, str))

    class Repeating(Client):
        def query(self, query):
            if "FROM Customer" in query:
                return {"QueryResponse": {"Customer": [contact("c1", "Alder")]}}
            return super().query(query)

    assert fetch(Repeating())["coverage"]["status"] == "partial"


def test_deadline_is_explicit_and_does_not_echo_provider_data(monkeypatch):
    monkeypatch.setattr(drivers, "OPERATION_SECONDS", -1)
    client = Client()
    result = fetch(client)
    assert result["coverage"]["status"] == "unavailable" and not client.calls
    assert result["coverage"]["issues"][0]["code"] == "operation_deadline"


@pytest.mark.parametrize("failure", ["size", "depth", "columns", "rows", "nested_data", "truncated"])
def test_directory_guard_rejects_bad_gl_before_the_shared_recorder_writes_it(monkeypatch, failure):
    source = report()
    if failure == "size":
        monkeypatch.setattr(drivers, "MAX_REPORT_BYTES", 20)
    elif failure == "depth":
        monkeypatch.setattr(drivers, "MAX_REPORT_DEPTH", 1)
        source["Rows"]["Row"] = [{"type": "Section", "Rows": {"Row": [
            {"type": "Section", "Rows": {"Row": [line()]}}
        ]}}]
    elif failure == "columns":
        source["Columns"]["Column"] = []
    elif failure == "rows":
        source["Rows"]["Row"][0]["ColData"] = []
    elif failure == "nested_data":
        source["Rows"]["Row"][0]["Rows"] = {"Row": [line()]}
    else:
        source["Rows"]["has_more"] = True
    writes = []
    runtime = SimpleNamespace(workspaces=SimpleNamespace(
        read_source=lambda _: {"reads": {}}, write_source=lambda scope, value: writes.append(value),
    ))
    scope = {"realm_id": "realm-a"}
    client = RecordedClient(runtime, scope, drivers.CatalogLimitClient(Client(source), "USD"), allow_driver_catalog=True)
    result = fetch(client)
    assert result["coverage"]["status"] == "unavailable"
    assert result["entries"] == [] and writes == []


def test_valid_guarded_reads_keep_authorized_inner_calls_and_captured_names():
    stored = {"reads": {}}
    writes = []

    def store(scope, value):
        stored.clear()
        stored.update(copy.deepcopy(value))
        writes.append(copy.deepcopy(value))

    runtime = SimpleNamespace(workspaces=SimpleNamespace(read_source=lambda _: copy.deepcopy(stored), write_source=store))
    inner = Client()
    client = RecordedClient(runtime, {"realm_id": "realm-a"}, drivers.CatalogLimitClient(inner, "USD"), allow_driver_catalog=True)
    result = fetch(client)
    assert result["coverage"]["status"] == "complete" and result["entries"][0]["name"] == "Alder"
    assert len(writes) == 3
    assert inner.calls[0][0] == "GeneralLedger"
    assert client.client.inner is inner


def test_default_clients_refuse_gl_before_source_transport_or_recording():
    runtime, active = route_runtime()
    runtime.settings = SimpleNamespace(get=lambda *_: None)
    native = NativeReadOnlyClient(runtime, active)
    touched = []
    native.legacy._require_bound = lambda: touched.append("authorization")
    with pytest.raises(QBOError, match="fixed cash-flow"):
        native.get_report("GeneralLedger")
    with pytest.raises(QBOError, match="Only cash-flow"):
        native._request("reports/GeneralLedger", {})
    assert not touched

    runtime.workspaces.read_source = lambda _: touched.append("read-source")
    runtime.workspaces.write_source = lambda *_: touched.append("write-source")
    for replay in (False, True):
        recorded = RecordedClient(runtime, active, Client(), replay=replay)
        with pytest.raises(QBOError, match="fixed cash-flow"):
            recorded.get_report("GeneralLedger")
    assert not touched


def test_gl_opt_in_retains_fixed_paths_company_currency_and_money_guards():
    import httpx

    runtime, active = route_runtime()
    runtime.settings = SimpleNamespace(get=lambda *_: None)
    scope = dict(active)
    native = NativeReadOnlyClient(runtime, scope, allow_driver_catalog=True)
    calls = []
    native.legacy._require_bound = lambda: calls.append("bound-check")
    bundle = {"realm_id": scope["realm_id"], "environment": "sandbox", "access_token": "synthetic-transport-only"}
    native.legacy._refresh = lambda **_: bundle
    response = report()

    def read(method, url, **kwargs):
        assert method == "GET"
        assert url == "https://sandbox-quickbooks.api.intuit.com/v3/company/realm-a/reports/GeneralLedger"
        assert kwargs["params"]["minorversion"] == "73"
        calls.append("http-read")
        return httpx.Response(200, json=response)

    native.legacy._http = read
    assert native.get_report("GeneralLedger")["Header"]["Currency"] == "USD"
    assert calls == ["bound-check", "http-read"]
    for name in ("../GeneralLedger", "GeneralLedger?x=y", "TrialBalance"):
        with pytest.raises(QBOError, match="fixed cash-flow"):
            native.get_report(name)
    with pytest.raises(QBOError, match="SELECT"):
        native.query("DELETE FROM Vendor")
    assert calls == ["bound-check", "http-read"]

    response["Header"]["Currency"] = "GBP"
    with pytest.raises(QBOError, match="USD currency"):
        native.get_report("GeneralLedger")
    response = report()
    response["Columns"]["Column"].append({"ColTitle": "Amount", "ColType": "Money"})
    response["Rows"]["Row"][0]["ColData"].append({"value": "0.001"})
    with pytest.raises(HTTPException, match="whole cents"):
        native.get_report("GeneralLedger")
    before = len(calls)
    bundle["realm_id"] = "different-company"
    with pytest.raises(QBOError, match="not bound"):
        native.get_report("GeneralLedger")
    assert len(calls) == before + 1  # Bound check happened; no HTTP read.
    active["revision"] += 1
    with pytest.raises(HTTPException, match="Workspace changed"):
        native.get_report("GeneralLedger")
    assert len(calls) == before + 1


def test_factory_gl_capability_defaults_off_for_synthetic_real_and_replay_clients():
    from pe_platform.backend.fixtures import dataset

    runtime, active = route_runtime()
    runtime.workspaces.read_source = lambda _: {"fixture": dataset(TODAY), "reads": {}}
    runtime.workspaces.write_source = lambda *_: None
    runtime.settings = SimpleNamespace(get=lambda *_: {"bound_company": True, "realm_id": active["realm_id"]})
    for synthetic in (False, True):
        scope = {**active, "synthetic": synthetic}
        default = client_for_scope(runtime, scope)
        assert not default.allow_driver_catalog
        with pytest.raises(QBOError, match="fixed cash-flow"):
            default.get_report("GeneralLedger")
        directory = client_for_scope(runtime, scope, allow_driver_catalog=True)
        assert directory.allow_driver_catalog
        if not synthetic:
            assert "GeneralLedger" in directory.client.allowed_reports
        else:
            assert directory.get_report("GeneralLedger", {
                "start_date": START.isoformat(), "end_date": TODAY.isoformat(), "accounting_method": "Accrual",
            })["Header"]["ReportName"] == "GeneralLedger"
    replay = client_for_scope(runtime, {**active, "synthetic": True}, replay=True)
    with pytest.raises(QBOError, match="fixed cash-flow"):
        replay.get_report("GeneralLedger")


def route_runtime():
    active = {"id": "workspace-a", "realm_id": "realm-a", "revision": 1, "currency": "USD"}

    def current(scope):
        if active != scope:
            raise HTTPException(409, "Workspace changed")

    def require(request):
        if request.headers.get("x-vesper-workspace") != active["id"] or request.headers.get("x-vesper-revision") != str(active["revision"]):
            raise HTTPException(409, "Workspace changed")
        return dict(active)

    return SimpleNamespace(financial_lock=threading.RLock(), require_scope=require,
                           workspaces=SimpleNamespace(require_current=current)), active


def test_route_requires_current_scope_company_and_discards_late_results(monkeypatch):
    runtime, active = route_runtime()
    client = Client()
    # Match the route's real date; the pure tests above pin the history window.
    end = date.today()
    client.gl = report([line(day=end)], StartPeriod=(end - timedelta(days=180)).isoformat(), EndPeriod=end.isoformat())
    def current_client(*_, **options):
        assert options == {"allow_driver_catalog": True}
        return client

    monkeypatch.setattr(drivers, "client_for_scope", current_client)
    app = FastAPI()
    drivers.register(app, runtime)
    headers = {"x-vesper-workspace": "workspace-a", "x-vesper-revision": "1"}
    with TestClient(app) as http:
        assert http.get("/cashflow-forecast/driver-catalog?company_id=other", headers=headers).status_code == 403
        assert http.get("/cashflow-forecast/driver-catalog?company_id=realm-a").status_code == 409
        assert not client.calls
        assert http.get("/cashflow-forecast/driver-catalog?company_id=realm-a", headers=headers).json()["coverage"]["status"] == "complete"
        original = client.get_report

        def late(*args):
            result = original(*args)
            active["revision"] += 1
            return result

        client.get_report = late
        refused = http.get("/cashflow-forecast/driver-catalog?company_id=realm-a", headers=headers)
        assert refused.status_code == 409 and "Alder" not in refused.text


def test_catalog_native_boundary_preserves_financial_fingerprint_in_isolated_process():
    result = subprocess.run([sys.executable, "-m", "pe_platform.tests.test_driver_catalog", "native"],
                            cwd=Path(__file__).resolve().parents[2],capture_output=True,text=True,timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr


def native_exercise():
    import tempfile
    from pe_platform.backend import bootstrap, cashflow
    directory = Path(tempfile.mkdtemp(prefix="vesper-gl-directory-test-"))
    runtime = bootstrap.configure(directory, port=8893, testing=True)
    app = FastAPI()
    bootstrap.register(app)
    with TestClient(app) as client:
        initial = client.get("/local/bootstrap").json()
        response = client.post("/local/demo", headers={"x-csrf-token": initial["csrf_token"]})
        assert response.status_code == 200, response.text
        scope = response.json()["workspace"]
        headers = {"x-csrf-token": initial["csrf_token"], "x-vesper-workspace": scope["id"], "x-vesper-revision": str(scope["revision"])}
        params = {"company_id": scope["realm_id"]}
        before = client.get("/cashflow-forecast/weekly", params=params, headers=headers).json()
        fingerprint = cashflow.financial_fingerprint(runtime, scope)
        export = cashflow.capture_export_snapshot(runtime, scope)
        fixture = copy.deepcopy(runtime.workspaces.read_source(scope)["fixture"])
        response = client.get("/cashflow-forecast/driver-catalog", params=params, headers=headers)
        assert response.status_code == 200, response.text
        catalog = response.json()
        assert catalog["coverage"]["status"] == "complete", catalog
        names = {entry["name"] for entry in catalog["entries"]}
        assert names >= {"Synthetic Beacon Services", "Synthetic historical legal", "Synthetic Alder Consulting"}
        assert "Synthetic unused master contact" not in names
        forecast_names = {row["name"] for row in before["customers"] + before["vendors"]}
        assert {"Synthetic Beacon Services", "Synthetic historical legal"}.isdisjoint(forecast_names)
        assert all(entry["active"] is False for entry in catalog["entries"] if entry["name"] in {"Synthetic Beacon Services", "Synthetic historical legal"})
        assert cashflow.financial_fingerprint(runtime, scope) == fingerprint
        assert runtime.workspaces.read_source(scope)["fixture"] == fixture
        assert cashflow.capture_export_snapshot(runtime, scope)["export_id"] == export["export_id"]
        from unittest.mock import patch
        from pe_platform.backend.fixtures import SyntheticQBOClient

        original_report = SyntheticQBOClient.get_report

        def failed_gl(self, name, params=None):
            if name == "GeneralLedger":
                raise ValueError("provider-secret-must-not-escape")
            return original_report(self, name, params)

        with patch.object(SyntheticQBOClient, "get_report", failed_gl):
            failed = client.get("/cashflow-forecast/driver-catalog", params=params, headers=headers)
            assert failed.status_code == 200 and failed.json()["coverage"]["status"] == "unavailable"
            assert "provider-secret-must-not-escape" not in failed.text
        assert cashflow.financial_fingerprint(runtime, scope) == fingerprint
        assert cashflow.capture_export_snapshot(runtime, scope)["export_id"] == export["export_id"]
        after = client.get("/cashflow-forecast/weekly", params=params, headers=headers).json()
        for key in ("customers", "vendors", "ending_balances", "total_cash_in", "total_cash_out", "export_id"):
            assert before[key] == after[key], key
        assert client.get("/cashflow-forecast/driver-catalog", params={"company_id": "other"}, headers=headers).status_code == 403
        selected = client.post("/local/workspaces", json={"name": "Other", "currency": "USD"}, headers=headers)
        assert selected.status_code == 200
        assert client.get("/cashflow-forecast/driver-catalog", params=params, headers=headers).status_code == 409


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    native_exercise()

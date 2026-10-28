from datetime import date

import httpx
import pytest

from vesper_cashflow.connectors.qbo import (
    QBOClient,
    QBOError,
    QBOIncomplete,
    parse_bank_ledger,
    parse_balance_sheet,
)
from vesper_cashflow.settings import MemorySecretStore, SettingsService


def ledger():
    return {
        "Header": {
            "ReportName": "GeneralLedger",
            "StartPeriod": "2026-10-01",
            "EndPeriod": "2026-10-31",
            "ReportBasis": "Accrual",
            "Currency": "USD",
        },
        "Columns": {
            "Column": [
                {"ColTitle": "Date"},
                {"ColTitle": "Memo/Description"},
                {"ColTitle": "Amount"},
                {"ColTitle": "Balance"},
            ]
        },
        "Rows": {
            "Row": [
                {
                    "type": "Section",
                    "Header": {"ColData": [{"value": "Synthetic Bank", "id": "10"}]},
                    "Rows": {
                        "Row": [
                            {
                                "type": "Data",
                                "ColData": [
                                    {"value": "Beginning Balance"},
                                    {"value": ""},
                                    {"value": ""},
                                    {"value": "100.00"},
                                ],
                            },
                            {
                                "type": "Data",
                                "ColData": [
                                    {"value": "2026-10-04"},
                                    {"value": "Synthetic deposit", "id": "txn-a"},
                                    {"value": "25.00"},
                                    {"value": "125.00"},
                                ],
                            },
                            {
                                "type": "Data",
                                "ColData": [
                                    {"value": "2026-10-04"},
                                    {"value": "Synthetic deposit", "id": "txn-a"},
                                    {"value": "25.00"},
                                    {"value": "150.00"},
                                ],
                            },
                        ]
                    },
                    "Summary": {
                        "ColData": [
                            {"value": "Total"},
                            {"value": ""},
                            {"value": "50.00"},
                            {"value": "150.00"},
                        ]
                    },
                }
            ]
        },
    }


def balance():
    return {
        "Header": {
            "ReportName": "BalanceSheet",
            "EndPeriod": "2026-10-28",
            "ReportBasis": "Accrual",
            "Currency": "USD",
        },
        "Columns": {
            "Column": [{"ColTitle": ""}, {"ColTitle": "Total", "ColType": "Money"}]
        },
        "Rows": {
            "Row": [
                {
                    "type": "Section",
                    "Header": {"ColData": [{"value": "Cash parent", "id": "10"}]},
                    "Rows": {
                        "Row": [
                            {
                                "type": "Data",
                                "ColData": [
                                    {"value": "Cash child", "id": "11"},
                                    {"value": "40.00"},
                                ],
                            }
                        ]
                    },
                    "Summary": {
                        "ColData": [{"value": "Total Cash"}, {"value": "150.00"}]
                    },
                }
            ]
        },
    }


def bank_accounts():
    return [
        {
            "Id": "10",
            "Name": "Synthetic parent",
            "AccountType": "Bank",
            "CurrencyRef": {"value": "USD"},
        },
        {
            "Id": "11",
            "Name": "Synthetic child",
            "AccountType": "Bank",
            "SubAccount": True,
            "ParentRef": {"value": "10"},
            "CurrencyRef": {"value": "USD"},
        },
    ]


def fixture_client(tmp_path, handler, *, bound=True):
    settings = SettingsService(tmp_path, MemorySecretStore(), testing=True)
    bundle = {
        "client_id": "synthetic-client",
        "client_secret": "synthetic-secret",
        "realm_id": "123",
        "refresh_token": "synthetic-old",
        "environment": "sandbox",
    }
    if bound:
        bundle["bound_company"] = {
            "id": "123",
            "name": "Synthetic Company",
            "environment": "sandbox",
        }
    settings.save("workspace-a", "qbo", bundle)
    requests = []

    def dispatch(request):
        requests.append(request)
        if request.url.host == "oauth.platform.intuit.com":
            return httpx.Response(
                200,
                json={
                    "access_token": "synthetic-access",
                    "refresh_token": "synthetic-rotated",
                    "expires_in": 3600,
                },
            )
        return handler(request)

    client = QBOClient(
        settings,
        "workspace-a",
        client=httpx.Client(transport=httpx.MockTransport(dispatch)),
        today=lambda: date(2026, 10, 28),
        sleep=lambda n: None,
    )
    return client, settings, requests


def test_ledger_exact_controls_preserve_duplicate_occurrences():
    result = parse_bank_ledger(
        ledger(), account_id="10", start_date="2026-10-01", end_date="2026-10-31"
    )
    assert (
        result["opening_balance"] == "100.00" and result["closing_balance"] == "150.00"
    )
    assert (
        len(result["rows"]) == 2 and result["rows"][0]["id"] != result["rows"][1]["id"]
    )
    assert result["rows"][0]["source_ids"] == ["txn-a"]
    assert result["source"]["activity"] == "50.00"


@pytest.mark.parametrize(
    "failure",
    [
        "foreign_account",
        "nested_account",
        "missing_opening",
        "bad_balance",
        "wrong_basis",
        "missing_column",
        "fault",
    ],
)
def test_unknown_incomplete_and_ambiguous_ledger_refused(failure):
    report = ledger()
    section = report["Rows"]["Row"][0]
    if failure == "foreign_account":
        section["Header"]["ColData"][0]["id"] = "999"
    elif failure == "nested_account":
        section["Rows"]["Row"].append(
            {"type": "Section", "Header": {"ColData": [{"id": "11"}]}}
        )
    elif failure == "missing_opening":
        section["Rows"]["Row"].pop(0)
    elif failure == "bad_balance":
        section["Summary"]["ColData"][-1]["value"] = "999.00"
    elif failure == "wrong_basis":
        report["Header"]["ReportBasis"] = "Cash"
    elif failure == "missing_column":
        report["Columns"]["Column"].pop()
    elif failure == "fault":
        report["Fault"] = {"Error": [{"Message": "synthetic-secret"}]}
    with pytest.raises(QBOIncomplete):
        parse_bank_ledger(
            report, account_id="10", start_date="2026-10-01", end_date="2026-10-31"
        )


def test_cash_anchor_avoids_parent_child_doublecount_and_missing_coverage():
    result = parse_balance_sheet(
        balance(), accounts=bank_accounts(), as_of="2026-10-28"
    )
    assert result["opening_cash"] == "150.00"
    assert {a["account_id"]: a["amount"] for a in result["accounts"]} == {
        "10": "110.00",
        "11": "40.00",
    }
    broken = balance()
    broken["Rows"]["Row"][0]["Rows"]["Row"] = []
    with pytest.raises(QBOIncomplete, match="missing bank-account"):
        parse_balance_sheet(broken, accounts=bank_accounts(), as_of="2026-10-28")
    foreign_currency = bank_accounts()
    foreign_currency[1]["CurrencyRef"]["value"] = "EUR"
    with pytest.raises(QBOIncomplete, match="Mixed-currency"):
        parse_balance_sheet(balance(), accounts=foreign_currency, as_of="2026-10-28")


def test_token_rotation_company_binding_and_fixed_read_hosts(tmp_path):
    # CompanyInfo.Id is normally 1 and is NOT the OAuth Realm ID.
    def handler(req):
        return httpx.Response(
            200, json={"CompanyInfo": {"Id": "1", "CompanyName": "Synthetic Company"}}
        )

    client, settings, requests = fixture_client(tmp_path, handler, bound=False)
    assert client.company()["id"] == "123"
    assert settings.get("workspace-a", "qbo")["refresh_token"] == "synthetic-rotated"
    with pytest.raises(QBOError, match="confirm"):
        client.accounts()
    client.bind("123")
    assert settings.public("workspace-a")["qbo"]["bound"]
    assert settings.get("workspace-b", "qbo") is None
    assert all(
        r.url.host in {"oauth.platform.intuit.com", "sandbox-quickbooks.api.intuit.com"}
        for r in requests
    )
    assert all(
        r.method == "GET" or r.url.host == "oauth.platform.intuit.com" for r in requests
    )
    with pytest.raises(QBOError, match="not allowed"):
        client._get("https://attacker.test/secret")


def test_exact_full_page_requires_next_page_and_count_after(tmp_path):
    records = [
        {
            "Id": str(i),
            "TxnDate": "2026-10-01",
            "Balance": "1.00",
            "DueDate": "2026-11-01",
        }
        for i in range(1, 1001)
    ]
    seen_queries = []

    def handler(req):
        query = req.url.params.get("query", "")
        seen_queries.append(query)
        if "COUNT(*)" in query:
            return httpx.Response(200, json={"QueryResponse": {"totalCount": 1000}})
        if "STARTPOSITION 1 " in query:
            return httpx.Response(
                200,
                json={
                    "QueryResponse": {
                        "Invoice": records,
                        "startPosition": 1,
                        "maxResults": 1000,
                    }
                },
            )
        return httpx.Response(200, json={"QueryResponse": {}})

    client, _, _ = fixture_client(tmp_path, handler)
    result = client._paged("Invoice", " WHERE Balance > '0'")
    assert len(result) == 1000
    assert any("STARTPOSITION 1001" in query for query in seen_queries)
    assert sum("COUNT(*)" in query for query in seen_queries) == 2


def test_missing_page_and_changed_counts_refuse_partial_data(tmp_path):
    counts = iter([2, 2])

    def missing(req):
        query = req.url.params.get("query", "")
        if "COUNT(*)" in query:
            return httpx.Response(
                200, json={"QueryResponse": {"totalCount": next(counts)}}
            )
        return httpx.Response(200, json={"QueryResponse": {"Bill": [{"Id": "1"}]}})

    client, _, _ = fixture_client(tmp_path, missing)
    with pytest.raises(QBOIncomplete, match="missing"):
        client._paged("Bill", " WHERE Balance > '0'")
    changes = iter([1, 2])

    def changed(req):
        if "COUNT(*)" in req.url.params.get("query", ""):
            return httpx.Response(
                200, json={"QueryResponse": {"totalCount": next(changes)}}
            )
        return httpx.Response(200, json={"QueryResponse": {"Bill": [{"Id": "1"}]}})

    client, _, _ = fixture_client(tmp_path, changed)
    with pytest.raises(QBOIncomplete, match="changed"):
        client._paged("Bill", " WHERE Balance > '0'")


def test_revoked_token_throttle_retries_and_secret_redaction(tmp_path):
    client, _, requests = fixture_client(
        tmp_path, lambda r: httpx.Response(429, json={"error": "synthetic-secret"})
    )
    with pytest.raises(QBOError) as error:
        client.company()
    assert "synthetic-secret" not in str(error.value)
    assert len([r for r in requests if r.method == "GET"]) == 3
    client.client = httpx.Client(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(400, json={"error": "synthetic-secret"})
        )
    )
    with pytest.raises(QBOError, match="grant"):
        client._refresh(force=True)


def test_forecast_dated_anchor_open_items_and_unreviewed_timing(tmp_path):
    def handler(req):
        if "reports/BalanceSheet" in req.url.path:
            assert req.url.params["end_date"] == "2026-10-28"
            return httpx.Response(200, json=balance())
        query = req.url.params.get("query", "")
        entity = (
            "Account"
            if "FROM Account" in query
            else "Invoice"
            if "FROM Invoice" in query
            else "Bill"
        )
        records = (
            bank_accounts()
            if entity == "Account"
            else [
                {
                    "Id": "5",
                    "TxnDate": "2026-10-01",
                    "DueDate": "2026-11-01",
                    "Balance": "12.34",
                    "CurrencyRef": {"value": "USD"},
                }
            ]
        )
        if "COUNT(*)" in query:
            return httpx.Response(
                200, json={"QueryResponse": {"totalCount": len(records)}}
            )
        return httpx.Response(200, json={"QueryResponse": {entity: records}})

    client, _, requests = fixture_client(tmp_path, handler)
    result = client.forecast_inputs("2026-10-28")
    assert result["opening_cash"] == "150.00"
    assert [i["amount"] for i in result["items"]] == ["12.34", "12.34"]
    assert result["coverage"]["assumptions"]["complete"] is False
    assert result["source"]["timing_review_required"] is True
    before = len(requests)
    with pytest.raises(QBOError, match="current-date"):
        client.forecast_inputs("2026-10-01")
    assert len(requests) == before

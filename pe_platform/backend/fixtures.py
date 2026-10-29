"""Labelled synthetic accounting transport; the upstream forecast does the math."""

from __future__ import annotations

import calendar
import copy
import re
from datetime import date, timedelta


def _month(today, months_back, day):
    index = today.year * 12 + today.month - 1 - months_back
    year, month = divmod(index, 12)
    return date(year, month + 1, min(day, calendar.monthrange(year, month + 1)[1]))


def dataset(today=None):
    today = today or date.today()
    friday = today + timedelta(days=(4 - today.weekday()) % 7)
    prior_friday = friday - timedelta(days=7)
    anchor = friday - timedelta(days=14)
    bank = {"value": "100", "name": "Synthetic checking"}
    rows = {
        name: []
        for name in (
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
        )
    }
    rows["Account"] = [
        {
            "Id": "100",
            "Name": "Synthetic checking",
            "FullyQualifiedName": "Synthetic checking",
            "AccountType": "Bank",
            "AccountSubType": "Checking",
            "Active": True,
            "CurrentBalance": "50500.00",
            "CurrencyRef": {"value": "USD"},
        }
    ]
    rows["Customer"] = [{"Id": "200", "DisplayName": "Synthetic Alder Consulting", "Active": True}]
    rows["Vendor"] = [
        {"Id": "300", "DisplayName": "Synthetic rent", "Active": True},
        {"Id": "301", "DisplayName": "Synthetic payroll", "Active": True},
        {"Id": "302", "DisplayName": "Synthetic insurance", "Active": True},
    ]
    customer = {"value": "200", "name": "Synthetic Alder Consulting"}
    rent = {"value": "300", "name": "Synthetic rent"}
    payroll = {"value": "301", "name": "Synthetic payroll"}
    for back in range(1, 7):
        due = _month(today, back, 15)
        iid = "10" + str(back)
        rows["Invoice"].append(
            {
                "Id": iid,
                "DocNumber": "SYN-PAID-" + iid,
                "TxnDate": (due - timedelta(days=30)).isoformat(),
                "DueDate": due.isoformat(),
                "CustomerRef": customer,
                "Balance": "0.00",
                "TotalAmt": "4000.00",
                "CurrencyRef": {"value": "USD"},
            }
        )
        rows["Payment"].append(
            {
                "Id": "20" + str(back),
                "TxnDate": (due + timedelta(days=2)).isoformat(),
                "CustomerRef": customer,
                "DepositToAccountRef": bank,
                "TotalAmt": "4000.00",
                "Line": [{"Amount": "4000.00", "LinkedTxn": [{"TxnId": iid, "TxnType": "Invoice"}]}],
            }
        )
        rent_date = _month(today, back, 1)
        rows["Bill"].append(
            {
                "Id": "30" + str(back),
                "TxnDate": (rent_date - timedelta(days=30)).isoformat(),
                "DueDate": rent_date.isoformat(),
                "VendorRef": rent,
                "Balance": "0.00",
                "TotalAmt": "2500.00",
                "CurrencyRef": {"value": "USD"},
            }
        )
        rows["BillPayment"].append(
            {
                "Id": "40" + str(back),
                "TxnDate": rent_date.isoformat(),
                "VendorRef": rent,
                "TotalAmt": "2500.00",
                "PayType": "Check",
                "CheckPayment": {"BankAccountRef": bank},
                "Line": [
                    {"Amount": "2500.00", "LinkedTxn": [{"TxnType": "Bill", "TxnId": "30" + str(back)}]}
                ],
            }
        )
    for back in range(2, 26):
        day = prior_friday - timedelta(weeks=back)
        rows["Purchase"].append(
            {
                "Id": "50" + str(back),
                "TxnDate": day.isoformat(),
                "AccountRef": bank,
                "EntityRef": payroll,
                "TotalAmt": "1000.00",
                "PaymentType": "Cash",
            }
        )
    rows["Invoice"].append(
        {
            "Id": "1100",
            "DocNumber": "SYN-OPEN-1100",
            "TxnDate": (friday - timedelta(days=30)).isoformat(),
            "DueDate": friday.isoformat(),
            "CustomerRef": customer,
            "Balance": "12000.00",
            "TotalAmt": "12000.00",
            "CurrencyRef": {"value": "USD"},
        }
    )
    rows["Bill"].append(
        {
            "Id": "3100",
            "DocNumber": "SYN-OPEN-3100",
            "TxnDate": (friday - timedelta(days=20)).isoformat(),
            "DueDate": (friday + timedelta(days=7)).isoformat(),
            "VendorRef": {"value": "302", "name": "Synthetic insurance"},
            "Balance": "6000.00",
            "TotalAmt": "6000.00",
            "CurrencyRef": {"value": "USD"},
        }
    )
    # One fully evidenced historical week: +2000 -1000 -500 = +500 cash.
    rows["Payment"].append(
        {
            "Id": "2100",
            "TxnDate": prior_friday.isoformat(),
            "CustomerRef": customer,
            "DepositToAccountRef": bank,
            "TotalAmt": "2000.00",
            "Line": [],
        }
    )
    rows["Purchase"].append(
        {
            "Id": "5100",
            "TxnDate": prior_friday.isoformat(),
            "AccountRef": bank,
            "EntityRef": payroll,
            "TotalAmt": "1000.00",
            "PaymentType": "Cash",
        }
    )
    rows["BillPayment"].append(
        {
            "Id": "4100",
            "TxnDate": prior_friday.isoformat(),
            "VendorRef": rent,
            "TotalAmt": "500.00",
            "PayType": "Check",
            "CheckPayment": {"BankAccountRef": bank},
            "Line": [],
        }
    )
    return {
        "label": "SYNTHETIC — fictional accounting source, no provider calls",
        "observed_date": today.isoformat(),
        "opening_anchor": anchor.isoformat(),
        "rows": rows,
    }


class SyntheticQBOClient:
    def __init__(self, scope, data):
        self.scope, self.data = scope, data
        self.realm_id = scope["realm_id"]

    def get_company_info(self):
        return {
            "CompanyInfo": {
                "Id": self.realm_id,
                "CompanyName": self.scope["name"],
                "LegalName": self.scope["name"],
                "Country": "US",
            }
        }

    def query(self, query):
        match = re.search(r"\bFROM\s+([A-Za-z]+)\b", query, re.I)
        if not match or match[1] not in self.data["rows"] or not re.match(r"^\s*SELECT\b", query, re.I):
            raise ValueError("Unsupported synthetic accounting read")
        entity = match[1]
        rows = copy.deepcopy(self.data["rows"][entity])
        if re.search(r"Balance\s*>\s*'?0", query, re.I):
            rows = [row for row in rows if float(row.get("Balance", 0)) > 0]
        if re.search(r"Balance\s*=\s*'?0", query, re.I):
            rows = [row for row in rows if float(row.get("Balance", 0)) == 0]
        for operator, raw in re.findall(r"TxnDate\s*(>=|<=|=|>|<)\s*'([0-9-]+)'", query):

            def accept(row):
                current = row.get("TxnDate", "")
                return {
                    ">=": current >= raw,
                    "<=": current <= raw,
                    "=": current == raw,
                    ">": current > raw,
                    "<": current < raw,
                }[operator]

            rows = [row for row in rows if accept(row)]
        identity = re.search(r"\bId\s*=\s*'([^']+)'", query, re.I)
        if identity:
            rows = [row for row in rows if str(row.get("Id")) == identity[1]]
        if re.search(r"Active\s*=\s*true", query, re.I):
            rows = [row for row in rows if row.get("Active", True)]
        rows.sort(key=lambda row: str(row.get("Id", "")))
        if re.search(r"COUNT\s*\(\s*\*\s*\)", query, re.I):
            return {"QueryResponse": {"totalCount": len(rows)}}
        start = re.search(r"STARTPOSITION\s+(\d+)", query, re.I)
        maximum = re.search(r"MAXRESULTS\s+(\d+)", query, re.I)
        offset = int(start[1]) - 1 if start else 0
        rows = rows[offset : offset + (int(maximum[1]) if maximum else 1000)]
        return {"QueryResponse": {entity: rows, "startPosition": offset + 1, "maxResults": len(rows)}}

    def get_report(self, report_name, params=None):
        params = params or {}
        if report_name in {"AgedReceivables", "AgedReceivableDetail", "AgedPayables", "AgedPayableDetail"}:
            receivable = report_name in {"AgedReceivables", "AgedReceivableDetail"}
            entity, ref = ("Invoice", "CustomerRef") if receivable else ("Bill", "VendorRef")
            as_of = date.fromisoformat(
                params.get("as_of_date", params.get("end_date", self.data["observed_date"]))
            )
            grouped = {}
            for item in self.data["rows"][entity]:
                balance = float(item.get("Balance", 0))
                if not balance:
                    continue
                party = item[ref]
                key = party["value"]
                row = grouped.setdefault(key, {"name": party["name"], "buckets": [0.0] * 5})
                overdue = (as_of - date.fromisoformat(item["DueDate"])).days
                index = (
                    0
                    if overdue <= 0
                    else 1
                    if overdue <= 30
                    else 2
                    if overdue <= 60
                    else 3
                    if overdue <= 90
                    else 4
                )
                row["buckets"][index] += balance
            return {
                "Header": {"ReportName": report_name, "Currency": "USD", "EndPeriod": as_of.isoformat()},
                "Columns": {
                    "Column": [{"ColTitle": "Customer" if receivable else "Vendor", "ColType": "Name"}]
                    + [
                        {"ColTitle": label, "ColType": "Money"}
                        for label in ("Current", "1-30", "31-60", "61-90", "91 and over", "Total")
                    ]
                },
                "Rows": {
                    "Row": [
                        {
                            "type": "Data",
                            "ColData": [{"value": row["name"], "id": key}]
                            + [
                                {"value": f"{amount:.2f}"}
                                for amount in [*row["buckets"], sum(row["buckets"])]
                            ],
                        }
                        for key, row in sorted(grouped.items())
                    ]
                },
            }
        if report_name == "ProfitAndLoss":
            # Fictional TTM source evidence for the native DSO/DPO summary engine.
            return {
                "Header": {"ReportName": report_name, "Currency": "USD"},
                "Columns": {
                    "Column": [
                        {"ColTitle": "", "ColType": "Account"},
                        {"ColTitle": "TOTAL", "ColType": "Money"},
                    ]
                },
                "Rows": {
                    "Row": [
                        {
                            "type": "Section",
                            "Rows": {"Row": []},
                            "Summary": {"ColData": [{"value": label}, {"value": amount}]},
                        }
                        for label, amount in (
                            ("Total Income", "48000.00"),
                            ("Total Cost of Goods Sold", "18000.00"),
                        )
                    ]
                },
            }
        if report_name != "BalanceSheet":
            raise ValueError("Unsupported synthetic cash/AR/AP accounting report.")
        end = params.get("end_date", self.data["opening_anchor"])
        balance = "50000.00" if end <= self.data["opening_anchor"] else "50500.00"
        return {
            "Header": {
                "ReportName": "BalanceSheet",
                "StartPeriod": params.get("start_date", end),
                "EndPeriod": end,
                "ReportBasis": "Accrual",
                "Currency": "USD",
                "Time": end + "T23:59:59Z",
            },
            "Columns": {
                "Column": [{"ColTitle": "", "ColType": "Account"}, {"ColTitle": "TOTAL", "ColType": "Money"}]
            },
            "Rows": {
                "Row": [
                    {
                        "type": "Section",
                        "Header": {"ColData": [{"value": "Bank Accounts"}, {"value": ""}]},
                        "Rows": {
                            "Row": [
                                {
                                    "type": "Data",
                                    "ColData": [
                                        {"value": "Synthetic checking", "id": "100"},
                                        {"value": balance},
                                    ],
                                }
                            ]
                        },
                        "Summary": {"ColData": [{"value": "Total Bank Accounts"}, {"value": balance}]},
                    }
                ]
            },
        }

    def get_bank_accounts(self):
        return self.query("SELECT * FROM Account WHERE Active = true")["QueryResponse"]["Account"]

    def get_transaction(self, entity, txn_id):
        rows = self.query(f"SELECT * FROM {entity} WHERE Id = '{txn_id}'")["QueryResponse"][entity]
        if not rows:
            raise ValueError("Synthetic transaction not found")
        return {entity: rows[0]}


def create_demo(runtime):
    from app.tenant_rls import tenant_scope
    from app.routers import cashflow_forecast as native

    scope = runtime.workspaces.create("Synthetic · PE cash-flow workspace", "USD", synthetic=True)
    runtime.workspaces.write_source(scope, {"reads": {}, "fixture": dataset()})
    with (
        tenant_scope(org_id=scope["organization_id"], realm_id=scope["realm_id"]),
        runtime.session_factory() as db,
    ):
        # Actual upstream acquisition/projection, not a scripted forecast payload.
        payload = native.build_weekly_forecast(company_id=scope["realm_id"], weeks=13, lookback=1, db=db)
        if payload.get("data_quality", {}).get("is_partial"):
            raise RuntimeError(
                "Synthetic native acquisition is incomplete; inspect the fixture, not fabricated zeros."
            )
    return scope

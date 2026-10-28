"""Conservative QBO read adapter. No write endpoints or guessed ledger reconstruction."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
import re
import threading
import time

import httpx

from ..settings import SettingsError

HOSTS = {
    "sandbox": "https://sandbox-quickbooks.api.intuit.com",
    "production": "https://quickbooks.api.intuit.com",
}
TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
MAX_RESPONSE = 8_000_000
MAX_ROWS = 25000
PAGE_SIZE = 1000


class QBOError(ValueError):
    pass


class QBOIncomplete(QBOError):
    pass


class QBOTooLarge(QBOIncomplete):
    pass


def _now():
    return datetime.now(timezone.utc).isoformat()


def _money(value):
    if isinstance(value, bool) or value is None or isinstance(value, float):
        raise QBOIncomplete("The accounting response contains an invalid exact amount.")
    text = str(value).strip().replace(",", "")
    if text.startswith("(") and text.endswith(")"):
        text = "-" + text[1:-1]
    if not re.fullmatch(r"-?[0-9]+(?:\.[0-9]{1,2})?", text) or len(text) > 24:
        raise QBOIncomplete(
            "The accounting response contains unsupported precision or amount formatting."
        )
    try:
        amount = Decimal(text)
        if not amount.is_finite():
            raise InvalidOperation
        return amount.quantize(Decimal("0.01"))
    except InvalidOperation:
        raise QBOIncomplete(
            "The accounting response contains an invalid amount."
        ) from None


def _currency(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Z]{3}", value):
        raise QBOIncomplete("The accounting report does not establish its currency.")
    return value


def _identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,80}", value):
        raise QBOIncomplete("The accounting response has an invalid record identifier.")
    return value


def _date(value):
    try:
        parsed = date.fromisoformat(value)
    except (ValueError, TypeError):
        raise QBOIncomplete(
            "The accounting response contains an invalid date."
        ) from None
    if parsed.isoformat() != value:
        raise QBOIncomplete("Use full ISO dates for accounting requests.")
    return parsed


def _digest(value):
    return sha256(
        json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode()
    ).hexdigest()


def _rows(node):
    if node is None:
        return []
    if not isinstance(node, dict) or not isinstance(node.get("Row", []), list):
        raise QBOIncomplete("The accounting report has an unknown row hierarchy.")
    return node.get("Row", [])


def _cells(node, count):
    cells = node.get("ColData")
    if (
        not isinstance(cells, list)
        or len(cells) != count
        or any(not isinstance(c, dict) for c in cells)
    ):
        raise QBOIncomplete("The accounting report has missing or unexpected columns.")
    return cells


def _report_header(report, name, end_date, start_date=None):
    if not isinstance(report, dict) or any(
        report.get(k) for k in ("Fault", "Warnings", "Truncated", "ContinuationToken")
    ):
        raise QBOIncomplete(
            "The accounting report is incomplete or contains a provider fault."
        )
    header = report.get("Header", {})
    if not isinstance(header, dict) or header.get("ReportName") != name:
        raise QBOIncomplete("The accounting report type does not match the request.")
    if (
        header.get("EndPeriod") != end_date
        or str(header.get("ReportBasis", "")).lower() != "accrual"
        or (start_date is not None and header.get("StartPeriod") != start_date)
    ):
        raise QBOIncomplete(
            "The accounting report date or accounting basis does not match the request."
        )
    return _currency(header.get("Currency"))


def _column_positions(report):
    columns = report.get("Columns", {}).get("Column")
    if not isinstance(columns, list) or not columns or len(columns) > 40:
        raise QBOIncomplete("The ledger does not declare its columns.")
    aliases = {
        "date": {"date", "tx date", "tx_date"},
        "amount": {"amount", "nat. amount", "subt_nat_amount"},
        "balance": {"balance", "rbal_amt"},
        "memo": {"memo/description", "memo", "description"},
        "type": {"transaction type", "txn_type", "type"},
        "name": {"name", "payee", "customer", "vendor"},
        "number": {"num", "doc_num", "no."},
    }
    positions = {}
    for i, column in enumerate(columns):
        if not isinstance(column, dict):
            raise QBOIncomplete("The ledger column declaration is malformed.")
        keys = {
            str(column.get("ColTitle", "")).strip().lower(),
            str(column.get("ColType", "")).strip().lower(),
        }
        for meta in column.get("MetaData", []):
            if meta.get("Name") == "ColKey":
                keys.add(str(meta.get("Value", "")).lower())
        for role, names in aliases.items():
            if keys & names:
                if role in positions:
                    raise QBOIncomplete(
                        "The ledger contains duplicate or ambiguous declared columns."
                    )
                positions[role] = i
    if not {"date", "amount", "balance"} <= positions.keys():
        raise QBOIncomplete(
            "The ledger needs unique declared date, signed amount and running-balance columns."
        )
    return len(columns), positions


def parse_bank_ledger(report, *, account_id, start_date, end_date):
    account_id = _identifier(account_id)
    start, end = _date(start_date), _date(end_date)
    currency = _report_header(report, "GeneralLedger", end_date, start_date)
    count, positions = _column_positions(report)
    sections = []

    def find_sections(rows, depth=0):
        if depth > 10:
            raise QBOIncomplete(
                "The ledger hierarchy is too deep to establish exact account controls."
            )
        for row in rows:
            if not isinstance(row, dict) or row.get("type") != "Section":
                raise QBOIncomplete(
                    "The ledger has data outside a controlled account section."
                )
            head = row.get("Header", {}).get("ColData", [])
            ids = {str(c["id"]) for c in head if isinstance(c, dict) and c.get("id")}
            if ids:
                if ids != {account_id}:
                    raise QBOIncomplete(
                        "The ledger includes another or ambiguous account. No data was replaced."
                    )
                sections.append(row)
            else:
                if row.get("Summary", {}).get("ColData"):
                    # Wrapper totals are not evidence for the requested account.
                    pass
                find_sections(_rows(row.get("Rows")), depth + 1)

    find_sections(_rows(report.get("Rows")))
    if len(sections) != 1:
        raise QBOIncomplete(
            "The ledger does not have one exact selected-account section with controls."
        )
    section = sections[0]
    opening = None
    running = None
    records = []
    for ordinal, row in enumerate(_rows(section.get("Rows")), 1):
        if not isinstance(row, dict) or row.get("type") != "Data" or row.get("Rows"):
            raise QBOIncomplete(
                "Nested subaccount ledger controls are ambiguous; use a supported exact-account export."
            )
        cells = _cells(row, count)
        date_text = str(cells[positions["date"]].get("value", "")).strip()
        labels = {str(c.get("value", "")).strip().lower() for c in cells}
        if "beginning balance" in labels or "opening balance" in labels:
            if (
                opening is not None
                or records
                or date_text not in {"", "Beginning Balance", "Opening Balance"}
            ):
                raise QBOIncomplete(
                    "The ledger has ambiguous opening-balance evidence."
                )
            opening = _money(cells[positions["balance"]].get("value"))
            running = opening
            if cells[positions["amount"]].get("value") not in {None, "", "0", "0.00"}:
                raise QBOIncomplete(
                    "The ledger opening row unexpectedly contains period activity."
                )
            continue
        if opening is None:
            raise QBOIncomplete("The ledger is missing a dated-period opening control.")
        when = _date(date_text)
        if not start <= when <= end:
            raise QBOIncomplete(
                "The ledger includes a row outside the requested period."
            )
        amount = _money(cells[positions["amount"]].get("value"))
        balance = _money(cells[positions["balance"]].get("value"))
        running += amount
        if running != balance:
            raise QBOIncomplete(
                "A ledger running balance does not agree with signed activity."
            )
        source_ids = sorted({str(c["id"]) for c in cells if c.get("id")})
        record_id = (
            "qbo-gl-" + _digest([account_id, start_date, end_date, ordinal, cells])[:28]
        )
        description = " | ".join(
            str(cells[positions[key]].get("value", ""))[:500]
            for key in ("type", "number", "name", "memo")
            if key in positions
        )
        records.append(
            {
                "id": record_id,
                "date": when.isoformat(),
                "description": description,
                "amount": str(amount),
                "source_ids": source_ids,
                "source_ordinal": ordinal,
            }
        )
        if len(records) > MAX_ROWS:
            raise QBOTooLarge("The ledger exceeds the controlled row budget.")
    if opening is None:
        raise QBOIncomplete(
            "A no-data ledger still needs an explicit opening and closing control."
        )
    summary = _cells(section.get("Summary", {}), count)
    closing = _money(summary[positions["balance"]].get("value"))
    activity = sum((_money(row["amount"]) for row in records), Decimal("0.00"))
    if running != closing or opening + activity != closing:
        raise QBOIncomplete(
            "Ledger opening plus activity does not equal its closing control."
        )
    summary_activity = summary[positions["amount"]].get("value")
    if summary_activity not in {None, ""} and _money(summary_activity) != activity:
        raise QBOIncomplete(
            "Ledger total activity does not agree with all parsed rows."
        )
    return {
        "rows": records,
        "opening_balance": str(opening),
        "closing_balance": str(closing),
        "currency": currency,
        "source": {
            "provider": "QBO",
            "report": "GeneralLedger",
            "account_id": account_id,
            "start_date": start_date,
            "end_date": end_date,
            "basis": "Accrual",
            "digest": _digest(report),
            "activity": str(activity),
            "row_count": len(records),
            "complete": True,
        },
    }


def parse_balance_sheet(report, *, accounts, as_of):
    currency = _report_header(report, "BalanceSheet", as_of)
    bank = {str(a["Id"]): a for a in accounts if a.get("AccountType") == "Bank"}
    if not bank:
        raise QBOIncomplete(
            "No bank-account coverage is available. Enter a dated cash snapshot explicitly."
        )
    if len(bank) != len([a for a in accounts if a.get("AccountType") == "Bank"]):
        raise QBOIncomplete("Account metadata contains duplicate bank identifiers.")
    for aid, account in bank.items():
        _identifier(aid)
        _currency(account.get("CurrencyRef", {}).get("value", currency))
        if account.get("CurrencyRef", {}).get("value", currency) != currency:
            raise QBOIncomplete("Mixed-currency bank aggregation is unsupported.")
        if (
            account.get("SubAccount")
            and account.get("ParentRef", {}).get("value") not in bank
        ):
            raise QBOIncomplete(
                "The bank hierarchy cannot be established from account metadata."
            )
    children = {
        aid: [
            bid
            for bid, b in bank.items()
            if str(b.get("ParentRef", {}).get("value", "")) == aid
        ]
        for aid in bank
    }
    for aid in bank:
        seen, cursor = set(), aid
        while cursor in bank:
            if cursor in seen:
                raise QBOIncomplete("Bank account metadata has a hierarchy cycle.")
            seen.add(cursor)
            cursor = str(bank[cursor].get("ParentRef", {}).get("value", ""))
    columns = report.get("Columns", {}).get("Column", [])
    if len(columns) != 2 or str(columns[1].get("ColType", "")).lower() != "money":
        raise QBOIncomplete(
            "The dated cash report does not have one unambiguous amount column."
        )
    found = {}

    def capture(aid, raw, inclusive):
        if aid in found:
            raise QBOIncomplete("The cash report repeats a bank account.")
        found[aid] = (_money(raw), inclusive)

    def walk(rows, depth=0):
        if depth > 15:
            raise QBOIncomplete("The cash report hierarchy is unsupported.")
        for row in rows:
            if not isinstance(row, dict):
                raise QBOIncomplete("The cash report contains malformed rows.")
            if row.get("type") == "Section":
                cells = row.get("Header", {}).get("ColData", [])
                aid = str(cells[0].get("id", "")) if cells else ""
                if aid in bank:
                    summary = _cells(row.get("Summary", {}), 2)
                    capture(aid, summary[1].get("value"), True)
                walk(_rows(row.get("Rows")), depth + 1)
            elif row.get("type") == "Data":
                cells = _cells(row, 2)
                aid = str(cells[0].get("id", ""))
                if aid in bank:
                    if row.get("Rows"):
                        raise QBOIncomplete(
                            "The cash report has an unknown data hierarchy."
                        )
                    capture(aid, cells[1].get("value"), False)
            else:
                raise QBOIncomplete("The cash report contains an unknown row type.")

    walk(_rows(report.get("Rows")))
    if set(found) != set(bank):
        raise QBOIncomplete(
            "The dated cash report is missing bank-account coverage. No balance was invented."
        )
    account_balances = []
    for aid, (amount, inclusive) in found.items():
        if children[aid] and not inclusive:
            raise QBOIncomplete("A parent bank balance has ambiguous child inclusion.")
        own = (
            amount - sum((found[child][0] for child in children[aid]), Decimal("0.00"))
            if inclusive
            else amount
        )
        account_balances.append(
            {
                "account_id": aid,
                "amount": str(own),
                "currency": currency,
                "basis": "account-only amount derived from dated report controls",
            }
        )
    total = sum((_money(a["amount"]) for a in account_balances), Decimal("0.00"))
    return {
        "opening_cash": str(total),
        "currency": currency,
        "accounts": account_balances,
        "digest": _digest(report),
    }


class QBOClient:
    def __init__(
        self,
        settings,
        workspace_id,
        *,
        client=None,
        lock=None,
        today=None,
        sleep=time.sleep,
    ):
        self.settings = settings
        self.workspace_id = workspace_id
        self.client = client
        self.lock = lock or threading.RLock()
        self.today = today or date.today
        self.sleep = sleep
        self.deadline = time.monotonic() + 120

    def _bundle(self):
        bundle = self.settings.get(self.workspace_id, "qbo")
        if not bundle or bundle.get("environment") not in HOSTS:
            raise QBOError("Set up a QBO connection in this workspace first.")
        return bundle

    def _http(self, method, url, **kwargs):
        owned = self.client is None
        client = self.client or httpx.Client(
            timeout=20, follow_redirects=False, trust_env=False
        )
        try:
            for attempt in range(3):
                remaining = self.deadline - time.monotonic()
                if remaining <= 0:
                    raise QBOError(
                        "The QBO read reached its time limit. Previous valid data is unchanged."
                    )
                response = client.request(
                    method, url, timeout=min(20, remaining), **kwargs
                )
                if response.status_code in {429, 500, 502, 503, 504} and attempt < 2:
                    delay = response.headers.get("Retry-After", "")
                    delay = (
                        min(int(delay), 4) if delay.isdigit() else 0.25 * (2**attempt)
                    )
                    self.sleep(delay)
                    continue
                return response
        except httpx.HTTPError:
            raise QBOError(
                "QBO could not be reached. Previous valid data is unchanged."
            ) from None
        finally:
            if owned:
                client.close()

    @staticmethod
    def _decode(response):
        if len(response.content) > MAX_RESPONSE or response.status_code == 413:
            raise QBOTooLarge("The accounting response exceeded its size budget.")
        if response.status_code in {401, 403}:
            raise QBOError(
                "QBO authorization expired or was revoked. Reconnect this workspace."
            )
        if response.status_code == 429:
            raise QBOError(
                "QBO is rate limiting requests. Wait and retry; prior data is preserved."
            )
        if response.status_code >= 400:
            raise QBOError(
                "QBO could not complete the read. Check the connection and try again."
            )
        try:
            value = json.loads(
                response.content,
                parse_float=Decimal,
                parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
            )
        except (ValueError, TypeError):
            raise QBOIncomplete(
                "QBO returned invalid JSON. Previous data is unchanged."
            ) from None
        if not isinstance(value, dict) or value.get("Fault"):
            raise QBOIncomplete("QBO returned a fault or incomplete source response.")
        return value

    def _refresh(self, *, force=False):
        with self.lock:
            bundle = self._bundle()
            if (
                not force
                and bundle.get("access_token")
                and bundle.get("expires_at", 0) > time.time() + 30
            ):
                return bundle
            response = self._http(
                "POST",
                TOKEN_URL,
                auth=(bundle["client_id"], bundle["client_secret"]),
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": bundle["refresh_token"],
                },
                headers={"Accept": "application/json"},
            )
            if response.status_code in {400, 401, 403}:
                raise QBOError(
                    "The QBO grant could not be refreshed. Reauthorize in the official Intuit Playground and reconnect."
                )
            data = self._decode(response)
            if not isinstance(data.get("access_token"), str) or not isinstance(
                data.get("refresh_token"), str
            ):
                raise QBOError(
                    "QBO did not return a complete rotated-token bundle. Reconnect before refreshing data."
                )
            expires = data.get("expires_in", 3600)
            if type(expires) is not int or not 1 <= expires <= 86400:
                raise QBOError("QBO returned an unsupported token lifetime.")
            bundle = {
                **bundle,
                "access_token": data["access_token"],
                "refresh_token": data["refresh_token"],
                "expires_at": time.time() + expires,
            }
            try:
                self.settings.replace(self.workspace_id, "qbo", bundle)
            except SettingsError:
                raise QBOError(
                    "The rotated QBO credentials could not be saved securely. Reconnect before another refresh."
                ) from None
            return bundle

    def _get(self, endpoint, *, params=None):
        # Only code-defined read routes are accepted, even from internal callers.
        if not re.fullmatch(
            r"(?:companyinfo/[0-9]{1,30}|query|reports/(?:GeneralLedger|BalanceSheet))",
            endpoint,
        ):
            raise QBOError("This accounting read endpoint is not allowed.")
        with self.lock:
            bundle = self._refresh()
            url = (
                HOSTS[bundle["environment"]]
                + "/v3/company/"
                + bundle["realm_id"]
                + "/"
                + endpoint
            )
            for attempt in range(2):
                response = self._http(
                    "GET",
                    url,
                    params={**(params or {}), "minorversion": "75"},
                    headers={
                        "Authorization": "Bearer " + bundle["access_token"],
                        "Accept": "application/json",
                    },
                )
                if response.status_code == 401 and attempt == 0:
                    bundle = self._refresh(force=True)
                    continue
                return self._decode(response)
            raise QBOError("QBO authorization needs to be renewed.")

    def company(self):
        bundle = self._bundle()
        data = self._get("companyinfo/" + bundle["realm_id"])
        company = data.get("CompanyInfo")
        if not isinstance(company, dict) or not company.get("Id"):
            raise QBOIncomplete(
                "QBO did not return CompanyInfo for the authorized Realm ID."
            )
        # CompanyInfo.Id is the CompanyInfo entity ID, not the OAuth Realm ID.
        # Realm binding comes from the fixed authenticated request path and grant.
        name = company.get("CompanyName") or company.get("LegalName")
        if not isinstance(name, str) or not name.strip():
            raise QBOIncomplete(
                "QBO did not identify the company for explicit binding."
            )
        return {
            "id": bundle["realm_id"],
            "name": name[:250],
            "environment": bundle["environment"],
        }

    def bind(self, company_id):
        company = self.company()
        if company_id != company["id"]:
            raise QBOError(
                "The displayed company does not match this authorization. Test the connection again."
            )
        with self.lock:
            bundle = {**self._bundle(), "bound_company": company}
            self.settings.replace(self.workspace_id, "qbo", bundle)
        return company

    def _require_bound(self):
        bundle = self._bundle()
        if bundle.get("bound_company", {}).get("id") != bundle["realm_id"]:
            raise QBOError(
                "Test the QBO connection and explicitly confirm the displayed company first."
            )

    def _count(self, entity, where):
        response = self._get(
            "query", params={"query": f"SELECT COUNT(*) FROM {entity}{where}"}
        )
        value = response.get("QueryResponse", {}).get("totalCount")
        if type(value) is not int or value < 0 or value > MAX_ROWS:
            raise QBOIncomplete(
                "QBO did not establish a supported complete source count."
            )
        return value

    def _paged(self, entity, where="", order="TxnDate"):
        if entity not in {"Account", "Invoice", "Bill"}:
            raise QBOError("This entity read is not allowed.")
        before = self._count(entity, where)
        collected, seen = [], set()
        start = 1
        while True:
            query = f"SELECT * FROM {entity}{where} ORDERBY {order} STARTPOSITION {start} MAXRESULTS {PAGE_SIZE}"
            data = self._get("query", params={"query": query})
            envelope = data.get("QueryResponse")
            if not isinstance(envelope, dict):
                raise QBOIncomplete("QBO omitted the paged source envelope.")
            rows = envelope.get(entity, [])
            if not isinstance(rows, list) or len(rows) > PAGE_SIZE:
                raise QBOIncomplete("QBO returned an invalid or oversized source page.")
            if "startPosition" in envelope and envelope["startPosition"] != start:
                raise QBOIncomplete("QBO returned the wrong page position.")
            if "maxResults" in envelope and envelope["maxResults"] != len(rows):
                raise QBOIncomplete("QBO returned inconsistent page controls.")
            for row in rows:
                if not isinstance(row, dict):
                    raise QBOIncomplete("QBO returned a malformed source record.")
                rid = _identifier(str(row.get("Id", "")))
                if rid in seen:
                    raise QBOIncomplete(
                        "QBO repeated a record across pages. Retry after the source is stable."
                    )
                seen.add(rid)
                collected.append(row)
            if len(collected) > before or len(collected) > MAX_ROWS:
                raise QBOIncomplete(
                    "QBO source population changed during the read. Previous data is unchanged."
                )
            # Full pages require the next page, including the exactly-full last page.
            if len(rows) < PAGE_SIZE:
                break
            start += PAGE_SIZE
            if start > MAX_ROWS + 1:
                raise QBOIncomplete("QBO pagination exceeded its source budget.")
        after = self._count(entity, where)
        if before != after or len(collected) != before:
            raise QBOIncomplete(
                "QBO source counts changed or pages are missing. Previous data is unchanged."
            )
        return collected

    def accounts(self):
        self._require_bound()
        return self._paged("Account", " WHERE Active IN (true, false)", order="Name")

    def bank_ledger(self, account_id, start_date, end_date):
        self._require_bound()
        account_id = _identifier(account_id)
        start, end = _date(start_date), _date(end_date)
        if end < start or (end - start).days > 366:
            raise QBOError("Choose a valid ledger period of at most one year.")
        accounts = self.accounts()
        account = next((a for a in accounts if str(a.get("Id")) == account_id), None)
        if any(
            str(a.get("ParentRef", {}).get("value", "")) == account_id for a in accounts
        ):
            raise QBOIncomplete(
                "Select a leaf bank account. Parent-account ledger detail can have ambiguous subaccount controls."
            )
        if account is None or account.get("AccountType") != "Bank":
            raise QBOError(
                "Select a bank account returned by this workspace's QBO connection."
            )
        observed_start, request_count = _now(), 0

        def fetch(left, right):
            nonlocal request_count
            request_count += 1
            if request_count > 380:
                raise QBOIncomplete("The ledger exceeded its bounded chunk budget.")
            if (right - left).days > 180:
                middle = left + timedelta(days=(right - left).days // 2)
                return combine(
                    fetch(left, middle), fetch(middle + timedelta(days=1), right)
                )
            try:
                report = self._get(
                    "reports/GeneralLedger",
                    params={
                        "account": account_id,
                        "start_date": left.isoformat(),
                        "end_date": right.isoformat(),
                        "accounting_method": "Accrual",
                    },
                )
                return parse_bank_ledger(
                    report,
                    account_id=account_id,
                    start_date=left.isoformat(),
                    end_date=right.isoformat(),
                )
            except QBOTooLarge:
                if left == right:
                    raise QBOIncomplete(
                        "A single-day ledger exceeds supported coverage. Previous data is unchanged."
                    ) from None
                middle = left + timedelta(days=(right - left).days // 2)
                return combine(
                    fetch(left, middle), fetch(middle + timedelta(days=1), right)
                )

        def combine(a, b):
            if (
                a["closing_balance"] != b["opening_balance"]
                or a["currency"] != b["currency"]
            ):
                raise QBOIncomplete(
                    "Ledger chunk boundary balances do not agree. No partial result was imported."
                )
            records = a["rows"] + b["rows"]
            if len(records) > MAX_ROWS:
                raise QBOIncomplete(
                    "The full ledger exceeds the controlled row budget."
                )
            return {
                "rows": records,
                "opening_balance": a["opening_balance"],
                "closing_balance": b["closing_balance"],
                "currency": a["currency"],
                "source": {
                    "provider": "QBO",
                    "report": "GeneralLedger",
                    "account_id": account_id,
                    "start_date": a["source"]["start_date"],
                    "end_date": b["source"]["end_date"],
                    "basis": "Accrual",
                    "complete": True,
                    "chunks": [a["source"], b["source"]],
                    "digest": _digest([a["source"], b["source"]]),
                    "row_count": len(records),
                },
            }

        result = fetch(start, end)
        declared_currency = account.get("CurrencyRef", {}).get(
            "value", result["currency"]
        )
        if declared_currency != result["currency"]:
            raise QBOIncomplete(
                "The ledger currency disagrees with the selected account."
            )
        result["source"].update(
            {
                "observed_start": observed_start,
                "observed_end": _now(),
                "observation": "Multiple provider reads; not an atomic vendor snapshot.",
            }
        )
        return result

    def forecast_inputs(self, as_of):
        self._require_bound()
        if _date(as_of) != self.today():
            raise QBOError(
                "Current QBO open balances support a current-date forecast only. Use a captured version or dated CSV snapshot for history."
            )
        began = _now()
        accounts_started = _now()
        accounts = self.accounts()
        accounts_end = _now()
        report = self._get(
            "reports/BalanceSheet",
            params={"end_date": as_of, "accounting_method": "Accrual"},
        )
        cash = parse_balance_sheet(report, accounts=accounts, as_of=as_of)
        coverage = {
            "cash": {
                "complete": True,
                "as_of": as_of,
                "accounts": cash["accounts"],
                "observed_start": accounts_started,
                "observed_end": _now(),
                "report_digest": cash["digest"],
            },
            "accounts": {
                "complete": True,
                "count": len(accounts),
                "observed_start": accounts_started,
                "observed_end": accounts_end,
            },
        }
        items = []
        for entity, direction in (("Invoice", "in"), ("Bill", "out")):
            observed_start = _now()
            rows = self._paged(entity, " WHERE Balance > '0'")
            missing_due_dates = []
            for row in rows:
                currency = row.get("CurrencyRef", {}).get("value", cash["currency"])
                if currency != cash["currency"]:
                    raise QBOIncomplete(
                        "Mixed-currency open balances cannot be silently aggregated."
                    )
                amount = _money(row.get("Balance"))
                if amount <= 0:
                    raise QBOIncomplete(
                        "An open-item response contains a nonpositive balance."
                    )
                rid = _identifier(str(row.get("Id", "")))
                due = row.get("DueDate") or None
                if due:
                    _date(due)
                else:
                    missing_due_dates.append(rid)
                transaction_date = row.get("TxnDate")
                if transaction_date:
                    _date(transaction_date)
                reference = row.get(
                    "CustomerRef" if entity == "Invoice" else "VendorRef", {}
                )
                items.append(
                    {
                        "id": "qbo-" + entity.lower() + "-" + rid,
                        "direction": direction,
                        "amount": str(amount),
                        "due_date": due,
                        "name": str(reference.get("name", "Unspecified counterparty"))[
                            :250
                        ],
                        "source": {
                            "provider": "QBO",
                            "entity": entity,
                            "id": rid,
                            "document": str(row.get("DocNumber", ""))[:100],
                            "transaction_date": transaction_date,
                            "currency": currency,
                            "as_of": as_of,
                            "timing": "Source due date; expected cash timing requires user review.",
                        },
                    }
                )
            coverage[entity.lower()] = {
                "complete": True,
                "count": len(rows),
                "missing_due_dates": missing_due_dates,
                "observed_start": observed_start,
                "observed_end": _now(),
            }
        coverage["assumptions"] = {
            "complete": False,
            "missing": ["payroll", "rent", "future sales", "financing", "taxes"],
            "message": "Enter explicit assumptions; unmodeled sections are not zero.",
        }
        return {
            "opening_cash": cash["opening_cash"],
            "currency": cash["currency"],
            "items": items,
            "coverage": coverage,
            "source": {
                "provider": "QBO",
                "as_of": as_of,
                "observed_start": began,
                "observed_end": _now(),
                "complete": True,
                "observation": "Multiple observations; not an atomic vendor snapshot.",
                "timing_review_required": True,
            },
        }

"""Sage Intacct REST monetary observations, with AP/report/book gaps explicit."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import re
from typing import Mapping

from .contracts import (CapabilityGap, CapabilityUnavailable, ParsedPage, SourceContractError,
                        SourceLine, SourceObservation, SourceScope, canonical_digest,
                        exact_decimal, required_text, strict_date)
from .transport import BoundedTransport, ProviderRead, strict_json_loads

CONTRACT_ID = "sage-intacct-rest1/monetary-journal-v1"
INDEX_CONTRACT_ID = "sage-intacct-rest1/query-journal-keys-v1"
SCHEMA_SHA256 = "f9e8740c9e8b7806e5aecfc60a78219703a450a79113e8e49defa53649320b57"
POSTING_STATES = frozenset({"posted", "reversed", "reversalPending"})
NONPOSTING_STATES = frozenset({"draft", "submitted", "partiallyApproved", "approved", "declined"})


def _key(value, name="key") -> str:
    value = required_text(value, name)
    if not re.fullmatch(r"[0-9]{1,30}", value):
        raise SourceContractError(f"{name}: bounded provider key required")
    return value


def make_read(scope: SourceScope, dataset: str = "gl", *, cursor: str | None = None) -> ProviderRead:
    if scope.provider != "sage_intacct":
        raise SourceContractError("wrong provider scope")
    if dataset != "gl":
        raise CapabilityUnavailable("intacct_ap_or_report_contract_open", dataset,
                                    "Full AP schema and completed report-output profile require review")
    start = int(cursor or "1") if re.fullmatch(r"\d{1,8}", cursor or "1") else 0
    if start < 1 or start > 100000:
        raise SourceContractError("Intacct query starts at one within bounded extent")
    # Fixed key-only discovery avoids inventing a related-line query projection.
    body = {"object": "general-ledger/journal-entry", "fields": ["key"],
            "filterParameters": {"includePrivate": False}, "orderBy": [{"key": "asc"}],
            "start": start, "size": 100}
    return ProviderRead(scope, "intacct.journal-keys", "POST", "/ia/api/v1/services/core/query",
                        body=body, contract_id=INDEX_CONTRACT_ID)


def make_object_read(scope: SourceScope, key: str) -> ProviderRead:
    if scope.provider != "sage_intacct":
        raise SourceContractError("wrong provider scope")
    return ProviderRead(scope, "intacct.journal-entry", "GET",
                        "/ia/api/v1/objects/general-ledger/journal-entry/" + _key(key),
                        contract_id=CONTRACT_ID)


def validate_operation(read: ProviderRead) -> None:
    if read.operation == "intacct.journal-keys" and isinstance(read.body, Mapping):
        expected = make_read(read.scope, cursor=str(read.body.get("start", "")))
    elif read.operation == "intacct.journal-entry":
        expected = make_object_read(read.scope, read.path.rsplit("/", 1)[-1])
    else:
        raise SourceContractError("unreviewed Intacct operation; no financial mutation route")
    if read != expected:
        raise SourceContractError("Intacct read differs from fixed monetary template")


def query_cursor(document: dict, *, start: int, count: int) -> str | None:
    meta = document.get("ia::meta")
    if not isinstance(meta, dict):
        raise SourceContractError("Intacct query metadata required")
    if any(type(meta.get(key)) is not int for key in ("totalCount", "start", "pageSize")):
        raise SourceContractError("integer total/start/pageSize metadata required")
    if meta["start"] != start or not 0 <= count <= meta["pageSize"] <= 4000 or meta["totalCount"] < 0:
        raise SourceContractError("inconsistent one-based query metadata")
    if "next" not in meta:
        raise SourceContractError("explicit terminal next metadata required")
    next_value = meta["next"]
    if next_value is None:
        if start - 1 + count != meta["totalCount"]:
            raise SourceContractError("terminal query does not account for totalCount")
        return None
    if type(next_value) is not int or next_value != start + count or count == 0 or next_value > meta["totalCount"]:
        raise SourceContractError("query next cursor repeated, skipped or nonadvancing")
    return str(next_value)


def _journal_records(scope: SourceScope, header: dict) -> tuple[SourceLine, ...]:
    key = _key(header.get("key"))
    state = header.get("state")
    if state not in POSTING_STATES | NONPOSTING_STATES:
        raise SourceContractError("explicit recognized journal state required; defaults are not observations")
    entity = header.get("entity")
    if not isinstance(entity, dict) or entity.get("id") != scope.external_entity_id:
        raise SourceContractError("journal entity differs from retained authorized entity")
    posted = strict_date(header.get("postingDate"), "postingDate")
    if state in NONPOSTING_STATES:
        return ()
    lines = header.get("lines")
    if not isinstance(lines, list) or len(lines) < 2:
        raise SourceContractError("complete monetary header lines required")
    result, seen = [], set()
    for item in lines:
        if not isinstance(item, dict):
            raise SourceContractError("monetary line object required")
        line_key = _key(item.get("key"), "line.key")
        if line_key in seen:
            raise SourceContractError("duplicate monetary line key")
        seen.add(line_key)
        if item.get("txnType") not in {"debit", "credit"}:
            raise SourceContractError("explicit monetary debit/credit required")
        if item.get("state") not in POSTING_STATES | {"reversal"}:
            raise SourceContractError("explicit recognized posted line state required")
        parent = item.get("journalEntry")
        if not isinstance(parent, dict) or parent.get("key") != key:
            raise SourceContractError("line/header identity mismatch")
        if not isinstance(item.get("txnAmount"), str) or not isinstance(item.get("baseAmount"), str):
            raise SourceContractError("monetary amounts require public-schema decimal strings")
        txn, base = exact_decimal(item["txnAmount"]), exact_decimal(item["baseAmount"])
        if txn < 0 or base < 0:
            raise SourceContractError("negative absolute amount needs a separately reviewed source profile")
        currency = item.get("currency")
        account = item.get("glAccount")
        if not isinstance(currency, dict) or currency.get("baseCurrency") != scope.currency:
            raise SourceContractError("base currency differs from selected book")
        txn_currency = currency.get("txnCurrency")
        if not isinstance(txn_currency, str) or not re.fullmatch(r"[A-Z]{3}", txn_currency):
            raise SourceContractError("explicit transaction currency required")
        if not isinstance(account, dict):
            raise SourceContractError("GL account identity required")
        account_id = required_text(account.get("id"), "glAccount.id")
        if not scope.period_start <= posted <= scope.period_end:
            continue
        rate = exact_decimal(currency["exchangeRate"]) if "exchangeRate" in currency else None
        result.append(SourceLine(key + ":" + line_key, f"journal/{key}/line/{line_key}", account_id,
                                 posted, base if item["txnType"] == "debit" else Decimal("0"),
                                 base if item["txnType"] == "credit" else Decimal("0"), scope.currency,
                                 source_version=canonical_digest(header), transaction_id=key, line_id=line_key,
                                 source_currency=txn_currency, source_amount=txn, rate=rate))
    return tuple(result)


def parse_page(scope: SourceScope, dataset: str, raw: bytes, *, cursor: str | None = None) -> ParsedPage:
    if scope.provider != "sage_intacct" or dataset != "gl":
        raise CapabilityUnavailable("intacct_ap_or_report_contract_open", dataset, "Only inspected monetary GL shapes are mapped")
    document = strict_json_loads(raw)
    if not isinstance(document, dict) or "ia::error" in document or "ia::result" not in document:
        raise SourceContractError("successful Intacct result envelope required")
    result = document["ia::result"]
    if isinstance(result, list):
        if len(result) > 4000 or any(not isinstance(item, dict) or set(item) != {"key"} for item in result):
            raise SourceContractError("fixed key-only discovery response required")
        keys = [_key(item["key"]) for item in result]
        if len(keys) != len(set(keys)):
            raise SourceContractError("duplicate query key")
        next_cursor = query_cursor(document, start=int(cursor or "1"), count=len(result))
        return ParsedPage((), next_cursor, next_cursor is None, hashlib.sha256(raw).hexdigest(),
                          INDEX_CONTRACT_ID, reported_count=document["ia::meta"]["totalCount"])
    if not isinstance(result, dict):
        raise SourceContractError("monetary journal result object required")
    if cursor is not None and result.get("key") != cursor:
        raise SourceContractError("monetary response key differs from requested retained key")
    records = _journal_records(scope, result)
    gap = CapabilityGap("intacct_book_and_report_profile_open", "gl",
                        "Monetary object observations do not prove reporting-book membership or independent report output")
    return ParsedPage(records, None, True, hashlib.sha256(raw).hexdigest(), CONTRACT_ID, (gap,))


def report_submission_state(status: int, raw: bytes) -> str:
    # 202 never earns completed report or qualification credit.
    document = strict_json_loads(raw)
    if status == 202 and isinstance(document, dict):
        return "pending-unqualified"
    raise CapabilityUnavailable("intacct_report_output_contract_open", "reports",
                                "No completed CSV/JSON column profile is approved")


class SageIntacctAdapter:
    def extract(self, scope: SourceScope, dataset: str, transport: BoundedTransport) -> tuple[ParsedPage, ...]:
        return tuple(observation.page for observation in self.extract_observations(scope, dataset, transport))

    def extract_observations(self, scope: SourceScope, dataset: str, transport: BoundedTransport):
        cursor, seen_cursors, seen_keys, total = None, set(), set(), None
        while True:
            start = datetime.now(timezone.utc)
            response, document = transport.send(make_read(scope, dataset, cursor=cursor))
            if response.status != 200:
                raise SourceContractError("query is not a completed monetary read")
            page = parse_page(scope, dataset, response.body, cursor=cursor)
            if total is not None and total != page.reported_count:
                raise SourceContractError("source query total drifted during extraction")
            total = page.reported_count
            transport.budget.account_page(0)
            yield SourceObservation(page, response.body, cursor, start, datetime.now(timezone.utc))
            for item in document["ia::result"]:
                key = _key(item["key"])
                if key in seen_keys:
                    raise SourceContractError("source identity repeated across discovery pages")
                seen_keys.add(key)
                start = datetime.now(timezone.utc)
                raw_response, _ = transport.send(make_object_read(scope, key))
                if raw_response.status != 200:
                    raise SourceContractError("journal read is not completed")
                journal_page = parse_page(scope, dataset, raw_response.body, cursor=key)
                transport.budget.account_page(len(journal_page.records))
                yield SourceObservation(journal_page, raw_response.body, key, start, datetime.now(timezone.utc))
            if page.terminal:
                return
            if page.next_cursor in seen_cursors:
                raise SourceContractError("repeated discovery cursor")
            seen_cursors.add(page.next_cursor)
            cursor = page.next_cursor

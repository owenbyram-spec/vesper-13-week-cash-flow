"""Xero 19.1.0 Journals observations; actual Journals grants remain external gates."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import re
from typing import Any
from uuid import UUID

from .contracts import (CapabilityGap, CapabilityUnavailable, ParsedPage, SourceContractError,
                        SourceLine, SourceObservation, SourceScope, canonical_digest, exact_decimal, required_text)
from .transport import BoundedTransport, ProviderRead, strict_json_loads

CONTRACT_ID = "xero-accounting-19.1.0/journals-v1"
SCHEMA_SHA256 = "1afca0717bb0d323210c0f88f3802684c068059f6e5161ee24ddde7ee6294a64"
SOURCE_TYPES = frozenset({"ACCREC", "ACCPAY", "ACCRECCREDIT", "ACCPAYCREDIT", "ACCRECPAYMENT",
    "ACCPAYPAYMENT", "ARCREDITPAYMENT", "APCREDITPAYMENT", "CASHREC", "CASHPAID", "TRANSFER",
    "ARPREPAYMENT", "APPREPAYMENT", "AROVERPAYMENT", "APOVERPAYMENT", "EXPCLAIM", "EXPPAYMENT",
    "MANJOURNAL", "PAYSLIP", "WAGEPAYABLE", "INTEGRATEDPAYROLLPE", "INTEGRATEDPAYROLLPT",
    "EXTERNALSPENDMONEY", "INTEGRATEDPAYROLLPTPAYMENT", "INTEGRATEDPAYROLLCN"})


def _uuid(value: Any, name: str) -> str:
    required_text(value, name)
    try:
        if str(UUID(value)) != value.lower():
            raise ValueError("noncanonical UUID")
    except (ValueError, AttributeError) as exc:
        raise SourceContractError(f"{name}: UUID required") from exc
    return value


def _journal_date(value: Any):
    if not isinstance(value, str):
        raise SourceContractError("JournalDate requires documented date encoding")
    matched = re.fullmatch(r"/Date\((-?\d+)(?:[+-]\d{4})?\)/", value)
    if not matched:
        raise SourceContractError("unknown JournalDate encoding")
    milliseconds = int(matched.group(1))
    if milliseconds % 86400000:
        raise SourceContractError("ambiguous nonmidnight JournalDate")
    try:
        return (datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=milliseconds)).date()
    except OverflowError as exc:
        raise SourceContractError("JournalDate out of range") from exc


def make_read(scope: SourceScope, dataset: str = "gl", *, cursor: str | None = None) -> ProviderRead:
    if scope.provider != "xero":
        raise SourceContractError("wrong provider scope")
    if dataset != "gl":
        raise CapabilityUnavailable("xero_dataset_mapping_unreviewed", dataset,
                                    "Detailed subledger/report column profile is not yet frozen for qualification")
    offset = cursor or "0"
    if not re.fullmatch(r"\d{1,15}", offset):
        raise SourceContractError("JournalNumber offset must be a bounded nonnegative integer")
    return ProviderRead(scope, "xero.journals", "GET", "/api.xro/2.0/Journals",
                        (("offset", offset), ("paymentsOnly", "true" if scope.basis == "cash" else "false")),
                        contract_id=CONTRACT_ID)


def validate_operation(read: ProviderRead) -> None:
    query = dict(read.query)
    if set(query) != {"offset", "paymentsOnly"}:
        raise SourceContractError("unreviewed Xero query")
    expected = make_read(read.scope, cursor=query["offset"])
    if read != expected:
        raise SourceContractError("Xero request differs from fixed read contract")


def parse_page(scope: SourceScope, dataset: str, raw: bytes, *, cursor: str | None = None) -> ParsedPage:
    make_read(scope, dataset, cursor=cursor)
    document = strict_json_loads(raw)
    if not isinstance(document, dict) or document.get("Status") != "OK" or not isinstance(document.get("Journals"), list):
        raise SourceContractError("successful Journals envelope required")
    journals = document["Journals"]
    if len(journals) > 100:
        raise SourceContractError("Journals page exceeds documented page size")
    last = int(cursor or "0")
    rows, gaps, seen = [], [], set()
    for journal in journals:
        if not isinstance(journal, dict):
            raise SourceContractError("Journal must be an object")
        number = journal.get("JournalNumber")
        if type(number) is not int or number <= last:
            raise SourceContractError("JournalNumber repeated, unordered or nonadvancing")
        last = number
        journal_id = _uuid(journal.get("JournalID"), "JournalID")
        posted = _journal_date(journal.get("JournalDate"))
        source_id, source_type = journal.get("SourceID", ""), journal.get("SourceType", "")
        if source_id:
            _uuid(source_id, "SourceID")
        if source_type and source_type not in SOURCE_TYPES:
            raise SourceContractError("unknown required journal source type")
        if not source_id:
            gaps.append(CapabilityGap("source_link_unavailable", "gl", "Provider journal has no source transaction link"))
        source_lines = journal.get("JournalLines")
        if not isinstance(source_lines, list) or not source_lines:
            raise SourceContractError("JournalLines required")
        for item in source_lines:
            if not isinstance(item, dict):
                raise SourceContractError("JournalLine must be an object")
            line_id = _uuid(item.get("JournalLineID"), "JournalLineID")
            account_id = _uuid(item.get("AccountID"), "AccountID")
            identity = (journal_id, line_id)
            if identity in seen:
                raise SourceContractError("duplicate journal/line identity")
            seen.add(identity)
            if isinstance(item.get("NetAmount"), bool) or not isinstance(item.get("NetAmount"), (Decimal, int)):
                raise SourceContractError("NetAmount must use the documented numeric JSON type")
            amount = exact_decimal(item["NetAmount"])
            if not scope.period_start <= posted <= scope.period_end:
                continue
            rows.append(SourceLine(journal_id + ":" + line_id, f"journal/{journal_id}/line/{line_id}",
                                   account_id, posted, max(amount, Decimal("0")), max(-amount, Decimal("0")),
                                   scope.currency, source_version=canonical_digest(journal), transaction_id=journal_id,
                                   line_id=line_id, source_link=source_id))
    terminal = not journals  # Short nonempty pages NEVER terminate Journals traversal.
    return ParsedPage(tuple(rows), None if terminal else str(last), terminal,
                      hashlib.sha256(raw).hexdigest(), CONTRACT_ID, tuple(gaps))


class XeroAdapter:
    def extract(self, scope: SourceScope, dataset: str, transport: BoundedTransport) -> tuple[ParsedPage, ...]:
        return tuple(observation.page for observation in self.extract_observations(scope, dataset, transport))

    def extract_observations(self, scope: SourceScope, dataset: str, transport: BoundedTransport):
        cursor, seen = None, set()
        while True:
            observed_start = datetime.now(timezone.utc)
            response, _ = transport.send(make_read(scope, dataset, cursor=cursor))
            if response.status != 200:
                raise SourceContractError("Journals requires completed response")
            page = parse_page(scope, dataset, response.body, cursor=cursor)
            transport.budget.account_page(len(page.records))
            yield SourceObservation(page, response.body, cursor, observed_start, datetime.now(timezone.utc))
            if page.terminal:
                return
            if page.next_cursor in seen:
                raise SourceContractError("repeated journal cursor")
            seen.add(page.next_cursor)
            cursor = page.next_cursor

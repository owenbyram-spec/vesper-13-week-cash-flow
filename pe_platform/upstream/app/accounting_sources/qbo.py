"""QBO modern read envelope with deliberately blocked unreviewed money mappings."""
from __future__ import annotations

import re
from urllib.parse import quote

from .contracts import CapabilityUnavailable, ParsedPage, SourceContractError, SourceScope
from .transport import BoundedTransport, ProviderRead, strict_json_loads

CONTRACT_ID = "qbo-reports-minor75/envelope-v1"
REPORTS = frozenset({"GeneralLedger", "TrialBalance", "ProfitAndLoss", "BalanceSheet"})


def make_report_read(scope: SourceScope, report: str = "GeneralLedger") -> ProviderRead:
    if scope.provider != "qbo" or report not in REPORTS or not re.fullmatch(r"\d{1,30}", scope.external_account_id):
        raise SourceContractError("bound QBO realm and supported report required")
    return ProviderRead(scope, "qbo.report." + report, "GET",
                        f"/v3/company/{quote(scope.external_account_id, safe='')}/reports/{report}",
                        (("minorversion", "75"), ("start_date", scope.period_start.isoformat()),
                         ("end_date", scope.period_end.isoformat()),
                         ("accounting_method", "Cash" if scope.basis == "cash" else "Accrual")),
                        contract_id=CONTRACT_ID)


def validate_operation(read: ProviderRead) -> None:
    report = read.operation.removeprefix("qbo.report.")
    if read != make_report_read(read.scope, report):
        raise SourceContractError("QBO read differs from exact report template")


def inspect_report(raw: bytes) -> tuple[tuple[str, tuple[str | None, ...]], ...]:
    """Retain nested physical occurrence and blanks; these are NOT mapped postings.

    Exact account/column/point-in-time contracts remain open. Never treat a blank
    as zero, or a row path as a stable source posting id.
    """
    document = strict_json_loads(raw)
    if not isinstance(document, dict) or not isinstance(document.get("Header"), dict) or not isinstance(document.get("Columns"), dict):
        raise SourceContractError("report header and columns required")
    root = document.get("Rows")
    if not isinstance(root, dict) or not isinstance(root.get("Row"), list):
        raise SourceContractError("report row envelope required")
    result = []

    def walk(rows, path):
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                raise SourceContractError("report row object required")
            occurrence = f"{path}/{index}"
            values = row.get("ColData")
            if values is not None:
                if not isinstance(values, list) or any(not isinstance(cell, dict) for cell in values):
                    raise SourceContractError("report cells required")
                cells = tuple(cell.get("value") for cell in values)
                if any(value is not None and not isinstance(value, str) for value in cells):
                    raise SourceContractError("report cell lexical values required")
                result.append((occurrence, cells))
            children = row.get("Rows")
            if children is not None:
                if not isinstance(children, dict) or not isinstance(children.get("Row"), list):
                    raise SourceContractError("nested report rows required")
                walk(children["Row"], occurrence)
    walk(root["Row"], "rows")
    return tuple(result)


def parse_page(scope: SourceScope, dataset: str, raw: bytes, *, cursor: str | None = None) -> ParsedPage:
    if scope.provider != "qbo":
        raise SourceContractError("wrong provider scope")
    raise CapabilityUnavailable("qbo_report_mapping_unreviewed", dataset,
                                "Exact monetary report metadata, account/line and independent tie-out profiles require review")


class QboAdapter:
    def extract(self, scope: SourceScope, dataset: str, transport: BoundedTransport) -> tuple[ParsedPage, ...]:
        # Refuse before a provider send, rather than produce pretend qualified GL.
        return (parse_page(scope, dataset, b""),)

    def extract_observations(self, scope: SourceScope, dataset: str, transport: BoundedTransport):
        parse_page(scope, dataset, b"")
        return iter(())

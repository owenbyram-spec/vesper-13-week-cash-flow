"""NetSuite contract gate. Public SuiteQL availability is not a granted schema."""
from __future__ import annotations

from .contracts import CapabilityUnavailable, ParsedPage, SourceContractError, SourceScope
from .transport import BoundedTransport, ProviderRead

CONTRACT_ID = "netsuite-suiteql/granted-role-contract-open"
MAX_QUERY_RESULTS = 100000


def require_partition(*, expected_rows: int, limit: int, offset: int) -> None:
    if any(type(value) is not int for value in (expected_rows, limit, offset)):
        raise SourceContractError("integer SuiteQL partition metadata required")
    if expected_rows < 0 or expected_rows >= MAX_QUERY_RESULTS:
        raise CapabilityUnavailable("netsuite_query_cap_partition", "gl", "Partition must be proven below 100000 query results")
    if not 1 <= limit <= 1000 or offset < 0 or offset % limit or offset + limit > MAX_QUERY_RESULTS:
        raise SourceContractError("invalid bounded SuiteQL paging")


def validate_operation(read: ProviderRead) -> None:
    raise CapabilityUnavailable("netsuite_role_contract_unreviewed", "gl",
                                "No SQL or field projection is approved until granted-role posting keys and grain are reviewed")


def parse_page(scope: SourceScope, dataset: str, raw: bytes, *, cursor: str | None = None) -> ParsedPage:
    if scope.provider != "netsuite":
        raise SourceContractError("wrong provider scope")
    raise CapabilityUnavailable("netsuite_role_contract_unreviewed", dataset,
                                "Posting-line key, role/book/subsidiary projection, deletion coverage and independent report remain open")


class NetSuiteAdapter:
    def extract(self, scope: SourceScope, dataset: str, transport: BoundedTransport) -> tuple[ParsedPage, ...]:
        return (parse_page(scope, dataset, b""),)

    def extract_observations(self, scope: SourceScope, dataset: str, transport: BoundedTransport):
        parse_page(scope, dataset, b"")
        return iter(())

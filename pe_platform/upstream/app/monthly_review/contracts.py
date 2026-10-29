"""Typed evidence and separately supplied controls for pure financial review.

V1 retains synthetic attestations. V2 describes origin and the relationships of
retained service/human receipts; it does not authenticate them. Only a trusted
service may resolve those controls from authorized retained records. Request or
document fields must never be deserialized as trusted ReceiptAuthority objects.
Neither version approves financial actions or establishes live completeness.
"""
from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass, fields, is_dataclass
from datetime import date, datetime
from decimal import Context, Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
import hashlib
import json
import re
from typing import Any

CHECK_IDS = ("accruals", "recurrence", "duplicates", "variance", "cfo")
CHECK_VERSION = "monthly-review-1"
STATUSES = frozenset({"candidate", "no_exception", "could_not_verify", "not_applicable",
                      "disabled", "immaterial", "error"})
SOURCE_ORIGINS = frozenset({"synthetic", "provider_extract", "document_revision", "manual_input"})


class ContractError(ValueError):
    """Invalid, ambiguous or cross-scope input; never a clean result."""


def money(value: Decimal | str | int) -> Decimal:
    """Accept exact finite numbers only; binary floats and booleans are refused."""
    if isinstance(value, bool) or not isinstance(value, (Decimal, str, int)):
        raise ContractError("money requires a finite Decimal, string or integer")
    try:
        result = Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise ContractError("invalid decimal") from exc
    if not result.is_finite() or len(result.as_tuple().digits) > 28 or abs(result.adjusted()) > 24:
        raise ContractError("nonfinite or out-of-range decimal")
    return result


def rounded(value: Decimal, quantum: Decimal = Decimal("0.01")) -> Decimal:
    with localcontext(Context(prec=50)):
        return value.quantize(quantum, rounding=ROUND_HALF_UP)


def period_bounds(period: str) -> tuple[date, date]:
    if not isinstance(period, str) or not re.fullmatch(r"\d{4}-\d{2}", period):
        raise ContractError("period must be YYYY-MM")
    try:
        year, month = (int(x) for x in period.split("-"))
        return date(year, month, 1), date(year, month, monthrange(year, month)[1])
    except ValueError as exc:
        raise ContractError("invalid period") from exc


def previous_period(period: str, months: int = 1) -> str:
    start, _ = period_bounds(period)
    index = start.year * 12 + start.month - 1 - months
    year, month = divmod(index, 12)
    if year < 1:
        raise ContractError("period precedes supported date range")
    return f"{year:04d}-{month + 1:02d}"


@dataclass(frozen=True)
class Scope:
    organization_id: str
    company_id: str
    source_system: str
    book: str
    basis: str
    currency: str
    period: str

    def __post_init__(self) -> None:
        if any(not isinstance(getattr(self, f.name), str) or not getattr(self, f.name).strip()
               for f in fields(self)):
            raise ContractError("scope fields must be nonempty strings")
        period_bounds(self.period)
        if not re.fullmatch(r"[A-Z]{3}", self.currency):
            raise ContractError("currency must be an explicit three-letter code")


@dataclass(frozen=True)
class SourceProvenance:
    """Physical source origin/digest, independently of qualification or approval."""

    origin: str
    content_digest: str


@dataclass(frozen=True)
class SourceRef:
    scope: Scope
    ref_id: str
    version: str
    locator: str
    page: str = ""
    section: str = ""
    excerpt_hash: str = ""
    provenance: SourceProvenance | None = None


@dataclass(frozen=True)
class LedgerLine:
    scope: Scope
    record_id: str
    version: str
    transaction_id: str
    line_id: str
    posted_on: date
    account_id: str
    counterparty_id: str
    debit: Decimal
    credit: Decimal
    source_ref: str
    document_id: str = ""
    obligation_id: str = ""
    service_start: date | None = None
    service_end: date | None = None
    kind: str = "expense"
    reverses_id: str = ""
    snapshot_local: bool = False

    @property
    def net(self) -> Decimal:
        with localcontext(Context(prec=50)):
            return self.debit - self.credit


@dataclass(frozen=True)
class APItem:
    scope: Scope
    record_id: str
    version: str
    source_ref: str
    counterparty_id: str
    document_id: str
    amount: Decimal
    service_start: date
    service_end: date
    obligation_id: str = ""
    posted_ledger_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class Obligation:
    scope: Scope
    record_id: str
    version: str
    source_ref: str
    counterparty_id: str
    account_id: str
    amount: Decimal
    service_start: date
    service_end: date
    allocation: str = "fixed_monthly"
    effective_on: date | None = None
    terminated_on: date | None = None
    service_refs: tuple[str, ...] = ()
    document_ids: tuple[str, ...] = ()
    metadata: tuple[tuple[str, str], ...] = ()  # informational; never read as controls


@dataclass(frozen=True)
class BudgetLine:
    scope: Scope
    record_id: str
    version: str
    source_ref: str
    account_id: str
    period: str
    amount: Decimal  # debit-positive, consistent with ledger net


@dataclass(frozen=True)
class CoverageReceipt:
    scope: Scope
    receipt_id: str
    dataset: str
    source_ref: str
    start: date
    end: date
    extracted_at: datetime
    completed: bool
    pagination_complete: bool
    reported_count: int
    reported_debit: Decimal
    reported_credit: Decimal
    source_debit: Decimal
    source_credit: Decimal
    mapping_complete: bool = True
    failures: tuple[str, ...] = ()
    origin: str = "synthetic"


@dataclass(frozen=True)
class ApprovalReceipt:
    scope: Scope
    receipt_id: str
    obligation_id: str
    obligation_version: str
    service_start: date
    service_end: date
    policy_version: str
    source_ref: str
    allowed_allocation: str = "fixed_monthly"
    allow_partial_month: bool = False
    origin: str = "synthetic"


@dataclass(frozen=True)
class ReceiptAuthority:
    """A retained record resolved by a trusted service, never a source assertion.

    The kernel verifies binding consistency only. Storage retention, actor identity,
    permissions and independent-human authentication belong to the service boundary.
    No constructor or label in this pure module grants that authority.
    """

    scope: Scope
    authority_id: str
    receipt_id: str
    receipt_kind: str
    receipt_digest: str
    evidence_digest: str
    policy_digest: str
    source_ref: str
    source_version: str
    source_digest: str
    assurance_state: str
    control_authority: str
    actor_id: str
    actor_kind: str
    preparer_actor_id: str = ""


@dataclass(frozen=True)
class ReviewControls:
    policy_version: str
    account_mappings: tuple[tuple[str, str], ...]  # revenue/cogs/opex/other
    enabled_checks: tuple[str, ...] = CHECK_IDS
    materiality: Decimal = Decimal("100.00")
    freshness_hours: int = 24
    recurrence_months: int = 3
    assurance_receipt_ids: tuple[str, ...] = ()
    approvals: tuple[ApprovalReceipt, ...] = ()
    currency_quantum: Decimal = Decimal("0.01")
    required_datasets: tuple[tuple[str, tuple[str, ...]], ...] = ()
    kpis: tuple[str, ...] = ("revenue", "cogs", "opex", "gross_profit", "gross_margin", "operating_profit")
    matching_rule: str = "explicit_references"
    variance_comparison: str = "both"
    provenance_version: str = "1"
    receipt_authorities: tuple[ReceiptAuthority, ...] = ()

    def mapping(self) -> dict[str, str]:
        return dict(self.account_mappings)


@dataclass(frozen=True)
class EvidenceBundle:
    scope: Scope
    snapshot_id: str
    sources: tuple[SourceRef, ...] = ()
    ledger: tuple[LedgerLine, ...] = ()
    ap: tuple[APItem, ...] = ()
    obligations: tuple[Obligation, ...] = ()
    budgets: tuple[BudgetLine, ...] = ()
    coverage: tuple[CoverageReceipt, ...] = ()
    metadata: tuple[tuple[str, str], ...] = ()  # untrusted informational data
    provenance_version: str = "1"


@dataclass(frozen=True)
class Finding:
    finding_id: str
    issue_family: str
    status: str
    message: str
    amount: Decimal | None = None
    source_refs: tuple[str, ...] = ()
    calculation: tuple[tuple[str, Any], ...] = ()
    missing_evidence: tuple[str, ...] = ()
    counterevidence: tuple[str, ...] = ()
    next_action: str = "Review evidence with the assigned preparer and independent approver."
    severity: str = "warning"


@dataclass(frozen=True)
class CheckResult:
    check_id: str
    status: str
    findings: tuple[Finding, ...] = ()
    missing_evidence: tuple[str, ...] = ()
    metrics: tuple[tuple[str, Any], ...] = ()
    coverage_refs: tuple[str, ...] = ()
    check_version: str = CHECK_VERSION


def result_status(findings: tuple[Finding, ...]) -> str:
    for status in ("could_not_verify", "error", "candidate", "immaterial"):
        if any(f.status == status for f in findings):
            return status
    return "no_exception"


def material_status(amount: Decimal, controls: ReviewControls) -> str:
    return "candidate" if abs(amount) >= controls.materiality else "immaterial"


def canonical_value(value: Any) -> Any:
    """Stable, exact JSON value; no user-supplied serializer or arbitrary object."""
    if isinstance(value, Decimal):
        if not value.is_finite() or len(value.as_tuple().digits) > 50 or abs(value.adjusted()) > 50:
            raise ContractError("nonfinite or out-of-range serialized decimal")
        if value == 0:
            return "0"
        with localcontext(Context(prec=50)):
            return format(value.normalize(), "f")
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if is_dataclass(value) and not isinstance(value, type):
        # Preserve the exact v1 byte contract, including existing fingerprints.
        omitted = set()
        if type(value) is SourceRef and value.provenance is None:
            omitted.add("provenance")
        if type(value) in {ReviewControls, EvidenceBundle} and value.provenance_version == "1":
            omitted.add("provenance_version")
        if type(value) is ReviewControls and not value.receipt_authorities:
            omitted.add("receipt_authorities")
        return {f.name: canonical_value(getattr(value, f.name)) for f in fields(value)
                if f.name not in omitted}
    if isinstance(value, dict):
        if any(not isinstance(k, str) for k in value):
            raise ContractError("JSON object keys must be strings")
        return {k: canonical_value(v) for k, v in sorted(value.items())}
    if isinstance(value, (tuple, list)):
        return [canonical_value(x) for x in value]
    if value is None or type(value) in (str, int, bool):
        return value
    raise ContractError(f"unsupported canonical type: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    return json.dumps(canonical_value(value), sort_keys=True, separators=(",", ":"), allow_nan=False)


def fingerprint(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def review_policy_digest(controls: ReviewControls) -> str:
    """Bind every policy value, excluding separately bound receipts/selection."""
    return fingerprint({field.name: getattr(controls, field.name) for field in fields(controls)
                        if field.name not in {"receipt_authorities", "approvals", "assurance_receipt_ids"}})


def validate_provenance(scope: Scope, controls: ReviewControls, bundle: EvidenceBundle) -> None:
    """Validate v1/v2 relationships; never authenticate a retained authority.

    The v2 authority binds the complete immutable evidence input and policy/mapping,
    not just a caller-selected receipt ID. Unselected coverage remains unqualified.
    The external service must establish that these retained records are authentic.
    """
    version = controls.provenance_version
    if type(version) is not str or version not in {"1", "2"} or bundle.provenance_version != version:
        raise ContractError("invalid or mismatched provenance version")
    if not isinstance(controls.receipt_authorities, tuple) or any(
        type(row) is not ReceiptAuthority for row in controls.receipt_authorities
    ):
        raise ContractError("receipt authorities must be immutable typed tuples")
    if version == "1":
        if controls.receipt_authorities or any(ref.provenance is not None for ref in bundle.sources):
            raise ContractError("v2 provenance requires an explicit version")
        return
    for ref in bundle.sources:
        provenance = ref.provenance
        if (type(provenance) is not SourceProvenance or type(provenance.origin) is not str
            or provenance.origin not in SOURCE_ORIGINS):
            raise ContractError("v2 source requires an explicit supported origin")
        if type(provenance.content_digest) is not str or not re.fullmatch(r"[0-9a-f]{64}", provenance.content_digest):
            raise ContractError("v2 source requires a content digest")
    sources = {ref.ref_id: ref for ref in bundle.sources}
    receipts = {("coverage", row.receipt_id): row for row in bundle.coverage}
    receipts.update({("approval", row.receipt_id): row for row in controls.approvals})
    for receipt in receipts.values():
        if not receipt.receipt_id or receipt.receipt_id != receipt.receipt_id.strip():
            raise ContractError("v2 receipt requires a canonical retained identity")
        if receipt.origin != sources[receipt.source_ref].provenance.origin:
            raise ContractError("receipt origin must match its retained source")
    selected = {("coverage", receipt_id) for receipt_id in controls.assurance_receipt_ids}
    selected.update(("approval", row.receipt_id) for row in controls.approvals)
    for authority in controls.receipt_authorities:
        if any(type(getattr(authority, field.name)) is not str for field in fields(authority)
               if field.name != "scope"):
            raise ContractError("receipt authority labels must be strings")
    authority_keys = [(row.receipt_kind, row.receipt_id) for row in controls.receipt_authorities]
    if (len(set(authority_keys)) != len(authority_keys)
        or len({row.authority_id for row in controls.receipt_authorities}) != len(authority_keys)):
        raise ContractError("duplicate retained receipt authority")
    if set(authority_keys) != selected or selected - receipts.keys():
        raise ContractError("accepted receipts require exactly matching retained authorities")
    evidence_digest = fingerprint(bundle)
    policy_digest = review_policy_digest(controls)
    for authority in controls.receipt_authorities:
        if type(authority.scope) is not Scope or authority.scope != scope:
            raise ContractError("receipt authority scope mismatch")
        if any(not getattr(authority, name) or getattr(authority, name) != getattr(authority, name).strip()
               for name in ("authority_id", "receipt_id", "actor_id")):
            raise ContractError("retained authority and actor identities are required")
        key = (authority.receipt_kind, authority.receipt_id)
        receipt = receipts[key]
        source = sources[receipt.source_ref]
        if (authority.receipt_digest != fingerprint(receipt)
            or authority.evidence_digest != evidence_digest
            or authority.policy_digest != policy_digest):
            raise ContractError("retained receipt digest mismatch")
        if (authority.source_ref != source.ref_id or authority.source_version != source.version
            or authority.source_digest != source.provenance.content_digest
            or receipt.origin != source.provenance.origin):
            raise ContractError("retained receipt source provenance mismatch")
        if authority.receipt_kind == "coverage":
            if (authority.control_authority != "service_qualification" or authority.actor_kind != "service"
                or authority.assurance_state not in {"reconciled", "human_reviewed"}
                or authority.preparer_actor_id):
                raise ContractError("coverage requires a retained service qualification")
        elif (authority.control_authority != "human_approval" or authority.actor_kind != "human"
              or authority.assurance_state != "human_reviewed" or not authority.preparer_actor_id
              or authority.preparer_actor_id != authority.preparer_actor_id.strip()
              or authority.preparer_actor_id == authority.actor_id):
            raise ContractError("approval requires a retained independent human decision")


def validate_inputs(scope: Scope, controls: ReviewControls, bundle: EvidenceBundle, as_of: datetime) -> None:
    """Fail before analysis when types, scope, source identity or controls conflict."""
    if not isinstance(scope, Scope) or not isinstance(controls, ReviewControls) or not isinstance(bundle, EvidenceBundle):
        raise ContractError("typed scope, controls and evidence are required")
    if not isinstance(as_of, datetime) or as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ContractError("as_of must have an explicit timezone")
    if bundle.scope != scope or not bundle.snapshot_id:
        raise ContractError("evidence scope mismatch or absent snapshot id")
    if not isinstance(controls.policy_version, str) or not controls.policy_version:
        raise ContractError("policy version must be a nonempty string")
    if not isinstance(controls.enabled_checks, tuple) or any(type(x) is not str for x in controls.enabled_checks):
        raise ContractError("enabled checks must be an immutable tuple of names")
    if set(controls.enabled_checks) - set(CHECK_IDS):
        raise ContractError("invalid policy version or check configuration")
    if len(set(controls.enabled_checks)) != len(controls.enabled_checks):
        raise ContractError("duplicate enabled check")
    if not isinstance(controls.materiality, Decimal) or not isinstance(controls.currency_quantum, Decimal):
        raise ContractError("typed control amounts must be Decimal")
    money(controls.materiality); money(controls.currency_quantum)
    if controls.materiality < 0 or controls.currency_quantum <= 0:
        raise ContractError("invalid materiality or rounding")
    if controls.currency_quantum != Decimal(1).scaleb(controls.currency_quantum.as_tuple().exponent):
        raise ContractError("rounding quantum must be a power of ten")
    if type(controls.freshness_hours) is not int or not 0 < controls.freshness_hours <= 8760:
        raise ContractError("invalid freshness target")
    if type(controls.recurrence_months) is not int or not 2 <= controls.recurrence_months <= 24:
        raise ContractError("invalid recurrence window")
    if not isinstance(controls.account_mappings, tuple) or any(not isinstance(row, tuple) or len(row) != 2 or any(type(x) is not str for x in row) for row in controls.account_mappings):
        raise ContractError("mappings must be immutable string pairs")
    mappings = controls.mapping()
    if len(mappings) != len(controls.account_mappings) or any(not k or v not in {"revenue", "cogs", "opex", "other"} for k,v in mappings.items()):
        raise ContractError("invalid or ambiguous account mapping")
    if controls.matching_rule != "explicit_references":
        raise ContractError("unsupported matching rule")
    if controls.variance_comparison not in {"prior", "budget", "both"}:
        raise ContractError("unsupported variance comparison")
    if len(set(controls.kpis)) != len(controls.kpis) or set(controls.kpis) - {"revenue","cogs","opex","gross_profit","gross_margin","operating_profit"}:
        raise ContractError("unsupported KPI configuration")
    if len(set(controls.assurance_receipt_ids)) != len(controls.assurance_receipt_ids):
        raise ContractError("duplicate accepted assurance receipt")
    if len(dict(controls.required_datasets)) != len(controls.required_datasets) or any(k not in CHECK_IDS for k,_ in controls.required_datasets):
        raise ContractError("invalid evidence requirement configuration")
    sources = {r.ref_id: r for r in bundle.sources}
    if len(sources) != len(bundle.sources):
        raise ContractError("duplicate source reference")
    for records, expected_type in ((bundle.sources, SourceRef), (bundle.ledger, LedgerLine),
                                   (bundle.ap, APItem), (bundle.obligations, Obligation),
                                   (bundle.budgets, BudgetLine), (bundle.coverage, CoverageReceipt),
                                   (controls.approvals, ApprovalReceipt)):
        if not isinstance(records, tuple) or any(type(r) is not expected_type for r in records):
            raise ContractError("evidence records must be immutable typed tuples")
    all_records = (*bundle.sources, *bundle.ledger, *bundle.ap, *bundle.obligations, *bundle.budgets, *bundle.coverage, *controls.approvals)
    for record in all_records:
        if record.scope != scope:
            raise ContractError("record scope mismatch")
        if isinstance(record, SourceRef):
            if any(type(v) is not str or not v for v in (record.ref_id, record.version, record.locator)):
                raise ContractError("source requires id, version and locator")
        else:
            if record.source_ref not in sources:
                raise ContractError("unresolved source reference")
            if hasattr(record, "version") and sources[record.source_ref].version != record.version:
                raise ContractError("source reference version mismatch")
        if hasattr(record, "record_id") and any(type(v) is not str or not v for v in (record.record_id, record.version)):
            raise ContractError("record requires id and version")
        for f in fields(record):
            value = getattr(record, f.name)
            if f.name in {"ref_id", "version", "locator", "page", "section", "excerpt_hash", "source_ref", "record_id",
                          "transaction_id", "line_id", "account_id", "counterparty_id", "document_id", "obligation_id",
                          "kind", "reverses_id", "receipt_id", "dataset", "origin", "policy_version", "allocation",
                          "obligation_version", "allowed_allocation"} and type(value) is not str:
                raise ContractError("record identifiers and labels must be strings")
            if f.name in {"amount", "debit", "credit", "reported_debit", "reported_credit", "source_debit", "source_credit"}:
                if not isinstance(value, Decimal):
                    raise ContractError("typed monetary fields must be Decimal")
                money(value)
            if isinstance(value, datetime) and (value.tzinfo is None or value.utcoffset() is None):
                raise ContractError("source datetime requires timezone")
    for records in (bundle.ap, bundle.obligations, bundle.budgets):
        ids = [r.record_id for r in records]
        if len(ids) != len(set(ids)):
            raise ContractError("duplicate or conflicting record identity")
    for line in bundle.ledger:
        if type(line.posted_on) is not date or not line.account_id or not line.counterparty_id:
            raise ContractError("ledger requires date, account and counterparty identity")
        if not line.transaction_id or not line.line_id or type(line.snapshot_local) is not bool:
            raise ContractError("ledger requires stable or explicit snapshot-local identity")
        if line.debit < 0 or line.credit < 0 or (line.debit and line.credit):
            raise ContractError("ledger line requires nonnegative, one-sided debit/credit")
        if line.kind not in {"expense", "accrual", "credit", "reversal", "prepayment_allocation", "payment", "other"}:
            raise ContractError("unsupported ledger line kind")
        if ((line.service_start is None) != (line.service_end is None)
            or (line.service_start is not None and (type(line.service_start) is not date or type(line.service_end) is not date))
            or (line.service_start and line.service_start > line.service_end)):
            raise ContractError("invalid ledger service period")
    for record in (*bundle.ap, *bundle.obligations, *controls.approvals):
        if type(record.service_start) is not date or type(record.service_end) is not date or record.service_start > record.service_end:
            raise ContractError("invalid service or approval period")
    ledger_ids = {line.record_id for line in bundle.ledger}
    for item in bundle.ap:
        if item.amount < 0:
            raise ContractError("AP amount must be nonnegative")
        if any(ref not in ledger_ids for ref in item.posted_ledger_ids):
            raise ContractError("unresolved AP-to-ledger reference")
        if not isinstance(item.posted_ledger_ids, tuple) or any(type(ref) is not str for ref in item.posted_ledger_ids):
            raise ContractError("AP-to-ledger references must be immutable strings")
    for obligation in bundle.obligations:
        if obligation.amount < 0 or not obligation.account_id or not obligation.counterparty_id:
            raise ContractError("invalid obligation")
        if any(ref not in sources for ref in obligation.service_refs):
            raise ContractError("unresolved service evidence reference")
        if any(value is not None and type(value) is not date for value in (obligation.effective_on, obligation.terminated_on)):
            raise ContractError("invalid obligation effective dates")
        if any(not isinstance(values, tuple) or any(type(x) is not str or not x for x in values)
               for values in (obligation.service_refs, obligation.document_ids)):
            raise ContractError("obligation references must be immutable string tuples")
    version = controls.provenance_version
    if type(version) is not str or version not in {"1", "2"} or bundle.provenance_version != version:
        raise ContractError("invalid or mismatched provenance version")
    for receipt in bundle.coverage:
        if type(receipt.start) is not date or type(receipt.end) is not date or not isinstance(receipt.extracted_at, datetime):
            raise ContractError("coverage requires explicit dates and extraction timestamp")
        if receipt.start > receipt.end or receipt.dataset not in {"gl", "ap", "obligations", "budget"}:
            raise ContractError("invalid coverage receipt")
        if controls.provenance_version == "1" and receipt.origin != "synthetic":
            raise ContractError("invalid or non-synthetic coverage receipt")
        if controls.provenance_version == "2" and receipt.origin not in SOURCE_ORIGINS:
            raise ContractError("invalid coverage origin")
        if type(receipt.reported_count) is not int or receipt.reported_count < 0:
            raise ContractError("invalid source row count")
        if any(type(getattr(receipt, k)) is not bool for k in ("completed","pagination_complete","mapping_complete")):
            raise ContractError("coverage flags must be booleans")
    for approval in controls.approvals:
        if not approval.receipt_id or not approval.obligation_id or not approval.obligation_version:
            raise ContractError("invalid approval receipt")
        if controls.provenance_version == "1" and approval.origin != "synthetic":
            raise ContractError("invalid or non-synthetic approval receipt")
        if controls.provenance_version == "2" and approval.origin not in SOURCE_ORIGINS:
            raise ContractError("invalid approval origin")
        if type(approval.allow_partial_month) is not bool or approval.allowed_allocation not in {"fixed_monthly", "actual_days"}:
            raise ContractError("invalid approval allocation control")
    if len({r.receipt_id for r in bundle.coverage}) != len(bundle.coverage):
        raise ContractError("duplicate assurance receipt")
    if len({r.receipt_id for r in controls.approvals}) != len(controls.approvals):
        raise ContractError("duplicate approval receipt")
    for budget in bundle.budgets:
        period_bounds(budget.period)
    validate_provenance(scope, controls, bundle)
    # Also validates metadata and prevents floats/arbitrary objects from entering hashes.
    canonical_json((scope, controls, bundle, as_of))

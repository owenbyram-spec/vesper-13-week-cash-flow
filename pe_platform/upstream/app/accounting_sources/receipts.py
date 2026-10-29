"""Authenticate retained deterministic qualification, never caller authority.

Native independent-report mappings remain capability gates. A closed synthetic
replay contract exists for future adversarial validation of the actual stored
writer/reader path; it cannot issue provider_extract or human_reviewed evidence.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import hashlib
from typing import Any

from sqlalchemy.orm import Session

from ..models import (AccountingSourceCoverageReceipt, AccountingSourceRun,
                      AccountingSourceSnapshot)
from ..monthly_review.contracts import (CoverageReceipt, Scope, canonical_value,
                                       fingerprint)
from .contracts import (CapabilityUnavailable, ParsedPage, SourceLine, SourceScope,
                        canonical_data, canonical_digest, exact_decimal, strict_date)
from .reconciliation import AccountTieOut, QualificationChecks, reconcile
from .snapshots import (SCHEMA_VERSION, PRODUCER_VERSION, SourceStateError, aware, lock_current_source,
                        owner_filter, read_page_bytes, retained_inventory, admit_retained_work,
                        source_scope_from_json, utc_now, _verified_run)
from .transport import strict_json_loads

SYNTHETIC_CONTRACT = "synthetic-source-replay/v1"
QUALIFIER_VERSION = "source-qualification/v1"
QUALIFIER_ID = "accounting-source-qualifier"
SUPPORTED_NATIVE_PROVIDERS = frozenset({"qbo", "xero", "netsuite", "sage_intacct"})


def assert_synthetic_admission(run: AccountingSourceRun, scope: SourceScope) -> None:
    admission = run.admission_json
    policy = run.policy_json
    if (scope.environment != "sandbox" or run.operation != "synthetic_replay"
        or type(admission) is not dict or admission.get("source_origin") != "synthetic"
        or type(policy) is not dict or policy.get("qualifier_contract") != SYNTHETIC_CONTRACT):
        raise SourceStateError("synthetic replay requires retained server-only admission")


def _keys(value: Any, required: set[str], name: str) -> dict[str, Any]:
    if type(value) is not dict or set(value) != required:
        raise SourceStateError(name + " schema mismatch")
    return value


def _synthetic_page(scope: SourceScope, dataset: str, raw: bytes, cursor: str | None) -> ParsedPage:
    payload = _keys(strict_json_loads(raw),
        {"schema", "dataset", "scope_digest", "records", "pagination", "independent_reports"}, "synthetic page")
    if (payload["schema"] != SYNTHETIC_CONTRACT or payload["dataset"] != dataset
        or payload["scope_digest"] != canonical_digest(scope) or dataset != "gl"):
        raise SourceStateError("synthetic source scope mismatch")
    pagination = _keys(payload["pagination"], {"cursor", "next_cursor", "terminal"}, "pagination")
    if pagination["cursor"] != cursor or type(pagination["terminal"]) is not bool:
        raise SourceStateError("synthetic cursor mismatch")
    if pagination["next_cursor"] is not None and (type(pagination["next_cursor"]) is not str or not pagination["next_cursor"]):
        raise SourceStateError("synthetic cursor is invalid")
    if type(payload["records"]) is not list or len(payload["records"]) > 100_000:
        raise SourceStateError("synthetic record inventory invalid")
    fields = {"source_id", "occurrence", "account_id", "posting_date", "debit", "credit", "currency",
              "source_version", "transaction_id", "line_id", "source_link", "source_currency",
              "source_amount", "rate", "reverses_id", "snapshot_local"}
    rows = []
    for item in payload["records"]:
        data = dict(_keys(item, fields, "synthetic record"))
        text_fields = fields - {"posting_date", "debit", "credit", "source_amount", "rate", "snapshot_local"}
        if any(type(data[key]) is not str for key in text_fields) or type(data["snapshot_local"]) is not bool:
            raise SourceStateError("synthetic source field type mismatch")
        data["posting_date"] = strict_date(data["posting_date"])
        for key in ("debit", "credit", "source_amount", "rate"):
            if data[key] is not None:
                data[key] = exact_decimal(data[key])
        rows.append(SourceLine(**data))
    return ParsedPage(tuple(rows), pagination["next_cursor"], pagination["terminal"],
                      hashlib.sha256(raw).hexdigest(), SYNTHETIC_CONTRACT, (), None)


def replay_source_page(scope: SourceScope, dataset: str, contract_id: str, raw: bytes,
                       *, cursor: str | None = None) -> ParsedPage:
    """Closed parser selection. No task, document or API can register a parser."""
    if contract_id == SYNTHETIC_CONTRACT:
        return _synthetic_page(scope, dataset, raw, cursor)
    if scope.provider not in SUPPORTED_NATIVE_PROVIDERS:
        raise SourceStateError("unsupported retained source provider")
    # Imports name reviewed local implementations only; no module name or URL
    # comes from source content. Native parsers refuse unreviewed field mappings.
    from . import netsuite, qbo, sage_intacct, xero
    parser = {"qbo": qbo.parse_page, "xero": xero.parse_page,
              "netsuite": netsuite.parse_page, "sage_intacct": sage_intacct.parse_page}[scope.provider]
    page = parser(scope, dataset, raw, cursor=cursor)
    if page.contract_id != contract_id:
        raise SourceStateError("retained provider contract version mismatch")
    return page


def kernel_scope(scope: SourceScope) -> Scope:
    if not scope.review_period:
        raise SourceStateError("review period required for kernel evidence")
    return Scope(str(scope.organization_id), str(scope.company_id), scope.provider,
                 str(scope.book_id), scope.basis, scope.currency, scope.review_period)


def coverage_receipt_from_json(value: dict[str, Any]) -> CoverageReceipt:
    expected = {"scope", "receipt_id", "dataset", "source_ref", "start", "end", "extracted_at",
                "completed", "pagination_complete", "reported_count", "reported_debit", "reported_credit",
                "source_debit", "source_credit", "mapping_complete", "failures", "origin"}
    data = dict(_keys(value, expected, "coverage receipt"))
    data["scope"] = Scope(**data["scope"])
    data["start"], data["end"] = strict_date(data["start"]), strict_date(data["end"])
    data["extracted_at"] = datetime.fromisoformat(data["extracted_at"])
    if data["extracted_at"].tzinfo is None:
        raise SourceStateError("coverage receipt timestamp requires timezone")
    for key in ("reported_debit", "reported_credit", "source_debit", "source_credit"):
        data[key] = exact_decimal(data[key])
    data["failures"] = tuple(data["failures"])
    return CoverageReceipt(**data)


def _synthetic_reconciliation(run, scope, pages):
    assert_synthetic_admission(run, scope)
    terminal = [p for p in pages if p.terminal]
    if len(terminal) != 1 or any(p.contract_id != SYNTHETIC_CONTRACT for p in pages):
        raise SourceStateError("synthetic qualification requires one complete partition")
    last = strict_json_loads(read_page_bytes(terminal[0]))
    report = _keys(last["independent_reports"], {"scope_digest", "accounts", "record_count", "debit", "credit",
        "revision_start", "revision_end", "successful_zero", "history_start", "history_end", "covered_datasets"},
        "independent synthetic report")
    if report["scope_digest"] != canonical_digest(scope):
        raise SourceStateError("independent report scope mismatch")
    if type(report["accounts"]) is not list or type(report["covered_datasets"]) is not list:
        raise SourceStateError("independent report inventory missing")
    if (type(report["revision_start"]) is not str or not report["revision_start"]
        or type(report["revision_end"]) is not str or not report["revision_end"]
        or type(report["successful_zero"]) is not bool):
        raise SourceStateError("independent report revision/zero evidence missing")
    accounts = []
    report_digest = canonical_digest(report)
    for item in report["accounts"]:
        row = _keys(item, {"account_id", "opening", "closing", "report_debit", "report_credit"}, "account report")
        accounts.append(AccountTieOut(row["account_id"], exact_decimal(row["opening"]), exact_decimal(row["closing"]),
            exact_decimal(row["report_debit"]), exact_decimal(row["report_credit"]), report_digest, report["scope_digest"]))
    lines = tuple(line for p in pages for line in replay_source_page(scope, p.dataset, p.contract_id,
                  read_page_bytes(p), cursor=p.cursor).records)
    policy = run.policy_json
    required = policy.get("required_datasets")
    if type(required) is not list or not required or any(item not in {"gl", "ap", "budget"} for item in required):
        raise SourceStateError("retained required dataset policy missing")
    history_start = strict_date(report["history_start"])
    history_end = strict_date(report["history_end"])
    changed = report["revision_start"] != report["revision_end"]
    account_ids = {account.account_id for account in accounts}
    checks = QualificationChecks(
        pagination_complete=True,
        mapping_complete=len(account_ids) == len(accounts)
                         and all(line.account_id in account_ids for line in lines),
        required_history_complete=history_start <= scope.period_start and history_end >= scope.period_end,
        required_datasets_complete=set(required) <= set(report["covered_datasets"]),
        independent_reports_complete=True, drift_checked=True, source_changed=changed,
        successful_zero_report=report["successful_zero"],
        point_in_time_required=policy.get("point_in_time_required") is True,
        point_in_time_proven=not changed,
        observed_start=min(datetime.fromisoformat(p.page_json["observed_start"]) for p in pages),
        observed_end=max(datetime.fromisoformat(p.page_json["observed_end"]) for p in pages),
        reported_count=report["record_count"], reported_debit=exact_decimal(report["debit"]),
        reported_credit=exact_decimal(report["credit"]), rounding_quantum=Decimal("0"),
    )
    return reconcile(scope, lines, tuple(accounts), checks), report_digest


def qualify_snapshot(db: Session, *, run_id: int, dispatch_permit: str,
                     snapshot_id: int, idempotency_key: str) -> AccountingSourceCoverageReceipt:
    """Retained admission → raw replay → reconciliation → immutable receipt.

    A failed qualifier leaves current snapshot/checkpoint untouched. Neither a
    caller's successful result nor a valid-looking digest can select an issuer.
    """
    run, scope = _verified_run(db, run_id, dispatch_permit)
    snapshot = db.query(AccountingSourceSnapshot).filter(*owner_filter(AccountingSourceSnapshot, scope),
        AccountingSourceSnapshot.id == snapshot_id, AccountingSourceSnapshot.run_id == run.id).one_or_none()
    if (snapshot is None or snapshot.epoch != run.epoch or snapshot.dataset != run.dataset
        or snapshot.schema_version != SCHEMA_VERSION or snapshot.producer_version != PRODUCER_VERSION
        or snapshot.content_digest != canonical_digest(snapshot.manifest_json)
        or aware(snapshot.issued_at) > utc_now() or aware(snapshot.expires_at) <= utc_now()):
        raise SourceStateError("snapshot binding mismatch")
    pages, records = retained_inventory(db, run, scope, expires_at=run.permit_expires_at)
    expected_pages = [{"id": p.id, "digest": p.content_digest, "raw_digest": p.raw_digest} for p in pages]
    expected_records = [{"id": r.id, "digest": r.content_digest} for r in records]
    if (snapshot.manifest_json.get("scope") != canonical_data(scope)
        or snapshot.manifest_json.get("pages") != expected_pages
        or snapshot.manifest_json.get("records") != expected_records
        or snapshot.manifest_json.get("run_id") != run.id
        or snapshot.manifest_json.get("epoch") != run.epoch
        or snapshot.manifest_json.get("lease_generation") != run.lease_generation):
        raise SourceStateError("snapshot inventory changed")
    if run.policy_json.get("qualifier_contract") != SYNTHETIC_CONTRACT:
        raise CapabilityUnavailable("independent_report_contract_unqualified", run.dataset,
            "Native source report, role, deletion and completeness mappings require reviewed conformance before qualification")
    result, report_digest = _synthetic_reconciliation(run, scope, pages)
    admit_retained_work(db, run.permit_expires_at)
    if not result.qualified:
        raise SourceStateError("source did not qualify: " + ",".join(result.failures))
    if not idempotency_key or len(idempotency_key) > 128:
        raise SourceStateError("canonical qualification idempotency key required")
    now = utc_now()
    source_ref = f"source:snapshot:{snapshot.id}:{run.dataset}"
    receipt = CoverageReceipt(kernel_scope(scope), f"qualification:{run.id}:{idempotency_key}", run.dataset,
        source_ref, scope.period_start, scope.period_end, aware(run.evaluation_at), True, True,
        result.record_count, result.debit, result.credit, result.debit, result.credit,
        True, (), "synthetic")
    qualification = {"snapshot_id": snapshot.id, "snapshot_digest": snapshot.content_digest,
        "run_id": run.id, "epoch": run.epoch, "scope_digest": canonical_digest(scope),
        "reconciliation": canonical_data(result), "report_digest": report_digest,
        "issuer_id": QUALIFIER_ID, "issuer_version": QUALIFIER_VERSION,
        "source_origin": "synthetic", "evaluation_at": aware(run.evaluation_at).isoformat()}
    digest = canonical_digest(qualification)
    existing = db.query(AccountingSourceCoverageReceipt).filter(
        *owner_filter(AccountingSourceCoverageReceipt, scope),
        AccountingSourceCoverageReceipt.idempotency_key == idempotency_key).one_or_none()
    if existing is not None:
        if existing.qualification_digest != digest or existing.receipt_digest != fingerprint(receipt):
            raise SourceStateError("qualification idempotency conflict")
        return existing
    _, _, head = lock_current_source(db, scope, run.dataset, expected_epoch=run.epoch,
                                   expires_at=run.permit_expires_at)
    if (head is None or head.lease_run_id != run.id or head.lease_generation != run.lease_generation
        or aware(head.lease_expires_at) <= now or aware(run.lease_expires_at) <= now):
        raise SourceStateError("qualification lease was lost")
    # This second helper uses already held locks. No immutable row is locked.
    max_age = run.policy_json.get("qualification_max_age_seconds", 86400)
    if type(max_age) is not int or not 1 <= max_age <= 86400:
        raise SourceStateError("invalid retained qualification freshness policy")
    expires = aware(run.evaluation_at) + timedelta(seconds=max_age)
    if expires <= now:
        raise SourceStateError("source qualification already expired")
    row = AccountingSourceCoverageReceipt(
        organization_id=scope.organization_id, organization_company_id=scope.company_id,
        connection_id=scope.connection_id, book_id=scope.book_id,
        run_id=run.id, snapshot_id=snapshot.id, epoch=run.epoch, dataset=run.dataset,
        source_ref=source_ref, source_version=snapshot.content_digest, source_digest=snapshot.content_digest,
        receipt_json=canonical_value(receipt), receipt_digest=fingerprint(receipt),
        qualification_json=qualification, qualification_digest=digest,
        issuer_id=QUALIFIER_ID, issuer_version=QUALIFIER_VERSION,
        schema_version=SCHEMA_VERSION, producer_version=QUALIFIER_VERSION,
        issued_at=now, expires_at=expires, idempotency_key=idempotency_key,
    )
    db.add(row)
    admit_retained_work(db, run.permit_expires_at)
    db.flush()
    from .connections import update_source_row
    update_source_row(db, head, {
        "current_snapshot_id": snapshot.id,
        "qualified_checkpoint_json": {"snapshot_id": snapshot.id, "qualification_id": row.id,
                                       "epoch": run.epoch, "digest": snapshot.content_digest},
        "revision": head.revision + 1})
    update_source_row(db, run, {"status": "qualified"})
    db.flush()
    return row

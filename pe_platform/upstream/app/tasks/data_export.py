"""
app/tasks/data_export.py
─────────────────────────
Celery task that serializes ONE organization's data into a JSON bundle plus
CSVs for the financial tables, zips it, stores it via the export storage
abstraction, and records an expiring signed download link on the
``TenantDataExport`` row.

Strict tenant isolation: every query is filtered through
``tenant_data_scope`` so a bundle can only ever contain the target org's rows.

Never serialized (hard exclusions, enforced field-by-field):
  * encrypted QBO tokens  — QBOToken.access_token / refresh_token
  * password hashes / credential material — User.password_hash
  * Stytch secrets and any session bearer material
  * pull-API key hashes, tenant LLM provider keys and the SMTP password
  * any other tenant's rows

The manifest lists every table the bundle deliberately leaves out
(``tenant_data_scope.EXCLUDED_TABLES``) with the reason, so a bundle never
reads as more complete than it is.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import zipfile
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from ..celery_app import celery_app
from ..config import settings
from ..db import SessionLocal
from .. import models
from ..services import tenant_export_storage as storage
from ..services.tenant_data_scope import EXCLUDED_TABLES, MODEL_SCOPES, resolve_tenant_scope, scoped_query
from ..services.redaction import redact_to_string
from ..tenant_rls import tenant_scope, tenant_scope_live

logger = logging.getLogger(__name__)


# Columns that must NEVER be serialized into an export bundle, keyed by table
# name. These are credential / token / secret material. The export builder drops
# them unconditionally; tests assert they never appear in any bundle file.
SENSITIVE_COLUMNS: dict[str, set[str]] = {
    "qbo_tokens": {"access_token", "refresh_token"},
    "users": {"password_hash", "pending_email_token_hash"},
    "qbo_oauth_states": {"state_hash"},
    "agent_confirmation_tokens": {"token_hash"},
    "approval_tokens": {"token_hash"},
    "auth_sessions": {"cookie_token_hint", "stytch_session_id"},
    "tenant_data_exports": {"download_token_hash"},
    # Exported as metadata only (key id, name, scopes, status, timestamps).
    "api_keys": {"token_hash"},
    "tenant_llm_credentials": {"anthropic_api_key_encrypted", "openai_api_key_encrypted"},
    "system_settings": {"email_smtp_pass"},
}

# W2: literal field contracts; future model columns stay denied by default.
REVIEW_EXPORT_COLUMNS.update({
    'accounting_source_runs': frozenset(('id', 'organization_id', 'organization_company_id', 'connection_id', 'book_id', 'period', 'dataset', 'epoch', 'lease_generation', 'lease_expires_at', 'permit_expires_at', 'admitted_user_id', 'admitted_membership_id', 'evaluation_at', 'status', 'operation', 'scope_json', 'scope_digest', 'policy_json', 'policy_digest', 'admission_digest', 'idempotency_key', 'schema_version', 'producer_version', 'created_at', 'updated_at')),
    'accounting_source_pages': frozenset(('id', 'organization_id', 'organization_company_id', 'connection_id', 'book_id', 'run_id', 'ordinal', 'dataset', 'partition_key', 'cursor', 'next_cursor', 'terminal', 'raw_digest', 'byte_count', 'contract_id', 'page_json', 'content_digest', 'schema_version', 'producer_version', 'created_at')),
    'accounting_source_records': frozenset(('id', 'organization_id', 'organization_company_id', 'connection_id', 'book_id', 'run_id', 'page_id', 'dataset', 'occurrence', 'source_id', 'source_version', 'content_digest', 'record_json', 'schema_version', 'producer_version', 'created_at')),
    'accounting_source_snapshots': frozenset(('id', 'organization_id', 'organization_company_id', 'connection_id', 'book_id', 'run_id', 'dataset', 'epoch', 'period', 'manifest_json', 'content_digest', 'issued_at', 'expires_at', 'idempotency_key', 'schema_version', 'producer_version', 'created_at')),
    'accounting_source_coverage_receipts': frozenset(('id', 'organization_id', 'organization_company_id', 'connection_id', 'book_id', 'snapshot_id', 'run_id', 'epoch', 'dataset', 'source_ref', 'source_version', 'source_digest', 'receipt_json', 'receipt_digest', 'qualification_json', 'qualification_digest', 'issuer_id', 'issuer_version', 'issued_at', 'expires_at', 'idempotency_key', 'schema_version', 'producer_version', 'created_at')),
    'accounting_source_control_bindings': frozenset(('id', 'organization_id', 'organization_company_id', 'connection_id', 'book_id', 'snapshot_id', 'qualification_id', 'epoch', 'bundle_digest', 'policy_digest', 'receipt_digest', 'source_digest', 'source_ref', 'source_version', 'binding_json', 'binding_digest', 'issuer_id', 'issuer_version', 'issued_at', 'expires_at', 'idempotency_key', 'schema_version', 'producer_version', 'created_at')),
    'accounting_source_heads': frozenset(('id', 'organization_id', 'organization_company_id', 'connection_id', 'book_id', 'period', 'dataset', 'epoch', 'revision', 'current_snapshot_id', 'lease_generation', 'lease_run_id', 'lease_expires_at', 'checkpoint_json', 'qualified_checkpoint_json', 'schema_version', 'producer_version', 'created_at', 'updated_at')),
    'accounting_source_invalidations': frozenset(('id', 'organization_id', 'organization_company_id', 'connection_id', 'book_id', 'period', 'dataset', 'epoch', 'reason', 'source_event_id', 'content_digest', 'schema_version', 'producer_version', 'created_at')),
    'accounting_source_events': frozenset(('id', 'organization_id', 'organization_company_id', 'connection_id', 'book_id', 'provider_event_id', 'entity_type', 'entity_id', 'operation', 'event_time', 'payload_json', 'content_digest', 'schema_version', 'producer_version', 'created_at')),
    'accounting_source_outbox': frozenset(('id', 'organization_id', 'organization_company_id', 'connection_id', 'book_id', 'run_id', 'event_id', 'operation', 'status', 'task_id', 'available_at', 'attempts', 'delivered_at', 'lease_expires_at', 'schema_version', 'producer_version', 'created_at', 'updated_at')),
    'accounting_source_oauth_states': frozenset(('id', 'organization_id', 'organization_company_id', 'connection_id', 'book_id', 'actor_user_id', 'membership_id', 'provider', 'environment', 'issuer', 'redirect_uri', 'expires_at', 'consumed_at', 'schema_version', 'producer_version', 'created_at', 'updated_at')),
})
SENSITIVE_COLUMNS.update({
    'accounting_source_runs': {'admission_json', 'request_json', 'dispatch_permit_hash'},
    'accounting_source_pages': {'payload_encrypted'},
    'accounting_source_outbox': {'payload_encrypted'},
    'accounting_source_oauth_states': {'binding_json', 'nonce_hash', 'code_verifier_encrypted', 'state_hash', 'session_hash'},
})


def _source_page_export_payload(row):
    """Required financial payload: verified lexical bytes, never metadata-only success."""
    from ..services.crypto import decrypt_secret
    import hashlib
    try:
        if not isinstance(row.payload_encrypted, str) or not row.payload_encrypted.startswith("enc:v1:"):
            raise ValueError("missing envelope")
        plaintext = decrypt_secret(row.payload_encrypted)
        if not isinstance(plaintext, str):
            raise ValueError("non-text payload")
        raw = plaintext.encode("utf-8", errors="strict")
        if (type(row.byte_count) is not int or len(raw) != row.byte_count
                or len(raw) > 8388608 or hashlib.sha256(raw).hexdigest() != row.raw_digest):
            raise ValueError("payload identity mismatch")
    except Exception:
        raise ValueError("Required retained source evidence is unavailable; complete export refused") from None
    return {"source_payload_utf8": plaintext, "source_payload_complete": True}


# Financial / QBO tables that additionally get a flat CSV rendering in the
# bundle (the JSON bundle still contains them too, for full fidelity).
CSV_TABLES = {
    "qbo_accounts",
    "qbo_vendors",
    "qbo_customers",
    "qbo_items",
    "qbo_journal_entries",
    "qbo_transactions",
    "qbo_gl_lines",
    "qbo_reports",
    "qbo_search_records",
    "monthly_financials",
    "budget_lines",
    "tenant_usage_events",
    "entity_billing",
    "tenant_billing",
}


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime,)):
        return value.isoformat()
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if hasattr(value, "tolist"):
        return value.tolist()  # pgvector embeddings arrive as numpy arrays
    return str(value)


def _row_to_dict(row: Any, sensitive: set[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for column in row.__table__.columns:
        if column.name in sensitive:
            continue  # credential / token material — never serialized
        out[column.name] = getattr(row, column.name)
    return out


def _serialize_review_tables(db, scope):
    """Export every canonical company/table under its own non-bypass pin.

    Counts are taken before materialization and reconciled after it in the same
    transaction. Owner and primary-key reconciliation reject dropped, duplicated
    or cross-company rows; unavailable counts never become successful emptiness.
    Real FORCE-RLS/non-BYPASSRLS behavior still requires the G1/AC-2 runtime oracle.
    """
    company_ids = review_company_ids(db, scope.organization_id)
    if set(company_ids) != set(scope.organization_company_ids):
        raise ValueError("Canonical company inventory changed during export")
    table_names = tuple(spec.model.__tablename__ for spec in REVIEW_MODEL_SCOPES)
    if len(set(table_names)) != len(table_names):
        raise ValueError("Duplicate review-table export registration")
    tables = {spec.model.__tablename__: [] for spec in REVIEW_MODEL_SCOPES}
    company_counts: dict[str, dict[str, int]] = {}
    for company_id in company_ids:
        counts: dict[str, int] = {}
        with tenant_scope_live(
            db, org_id=scope.organization_id, company_id=company_id, realm_id=None, bypass=False,
        ):
            for spec in REVIEW_MODEL_SCOPES:
                table = spec.model.__tablename__
                query = review_company_query(
                    db, spec, organization_id=scope.organization_id, company_id=company_id,
                )
                expected = review_row_count(query)
                rows = query.order_by(spec.model.id).all()
                if len(rows) != expected or review_row_count(query) != expected:
                    raise ValueError("Incomplete review company/table export")
                ids: set[int] = set()
                sensitive = set(SENSITIVE_COLUMNS.get(table, set()))
                public_columns = REVIEW_EXPORT_COLUMNS.get(table)
                if public_columns is None:
                    raise ValueError("Review table has no reviewed export field contract")
                sensitive.update(
                    column.name for column in spec.model.__table__.columns
                    if column.name not in public_columns
                )
                for row in rows:
                    if (
                        row.organization_id != scope.organization_id or row.organization_company_id != company_id
                        or isinstance(row.id, bool) or not isinstance(row.id, int) or row.id <= 0
                        or row.id in ids
                    ):
                        raise ValueError("Review export ownership/identity mismatch")
                    ids.add(row.id)
                    serialized_row = _row_to_dict(row, sensitive)
                    if table == "accounting_source_pages":
                        serialized_row.update(_source_page_export_payload(row))
                    tables[table].append(serialized_row)
                counts[table] = expected
        company_counts[str(company_id)] = counts
    if review_company_ids(db, scope.organization_id) != company_ids:
        raise ValueError("Canonical company inventory changed during export")
    return tables, company_counts


def serialize_organization(db, organization_id: int) -> dict[str, Any]:
    """Build the in-memory export structure for one organization.

    Returns a dict with ``bundle`` (table_name -> list[row dict]) and
    ``row_counts`` (table_name -> int). Shared / global tables are excluded;
    only org-scoped rows are included, filtered through the tenant scope.
    """
    scope = resolve_tenant_scope(db, organization_id)

    bundle: dict[str, list[dict[str, Any]]] = {}
    row_counts: dict[str, int] = {}

    # The organization root row itself (the tenant identity).
    org = db.query(models.Organization).filter(models.Organization.id == organization_id).first()
    if org is not None:
        bundle["organizations"] = [_row_to_dict(org, SENSITIVE_COLUMNS.get("organizations", set()))]
        row_counts["organizations"] = 1

    for spec in MODEL_SCOPES:
        if spec.review_owned:
            continue  # separately enumerated under exact numeric-company pins
        table = spec.model.__tablename__
        sensitive = SENSITIVE_COLUMNS.get(table, set())
        if spec.model is models.FirmOperationsSnapshot:
            # This table deliberately has no bypass policy. Pin its exact owner
            # for the query AND serialization, then restore the lifecycle scope.
            with tenant_scope_live(db, org_id=organization_id, realm_id=None, bypass=False):
                rows = scoped_query(db, spec, scope).all()
                bundle[table] = [_row_to_dict(r, sensitive) for r in rows]
                row_counts[table] = len(rows)
        else:
            rows = scoped_query(db, spec, scope).all()
            bundle[table] = [_row_to_dict(r, sensitive) for r in rows]
            row_counts[table] = len(rows)

    review_tables, company_row_counts = _serialize_review_tables(db, scope)
    bundle.update(review_tables)
    row_counts.update({table: len(rows) for table, rows in review_tables.items()})

    return {
        "scope": {
            "organization_id": organization_id,
            "realm_ids": list(scope.realm_ids),
            "organization_company_ids": list(scope.organization_company_ids),
        },
        "bundle": bundle,
        "row_counts": row_counts,
        "company_row_counts": company_row_counts,
    }


def build_zip_bytes(organization_id: int, serialized: dict[str, Any]) -> bytes:
    """Render the serialized structure into a zip of JSON + CSV files."""
    bundle = serialized["bundle"]
    row_counts = serialized["row_counts"]

    manifest = {
        "export_format_version": "1",
        "organization_id": organization_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scope": serialized["scope"],
        "row_counts": row_counts,
        "company_row_counts": serialized["company_row_counts"],
        "excluded_sensitive_columns": {k: sorted(v) for k, v in SENSITIVE_COLUMNS.items()},
        "excluded_tables": {
            table: reason for table, reason in sorted(EXCLUDED_TABLES.items()) if table not in bundle
        },
        "note": (
            "Encrypted QBO tokens, password hashes, API key hashes, LLM provider keys "
            "and Stytch/session secrets are never included in this bundle. Tables "
            "under excluded_tables are deliberately not exported, for the reason given."
        ),
    }

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, indent=2, default=_json_default))
        zf.writestr(
            "bundle.json",
            json.dumps(bundle, indent=2, default=_json_default, sort_keys=True),
        )
        for table in sorted(CSV_TABLES):
            rows = bundle.get(table) or []
            if not rows:
                continue
            fieldnames: list[str] = []
            for row in rows:
                for key in row.keys():
                    if key not in fieldnames:
                        fieldnames.append(key)
            csv_buf = io.StringIO()
            writer = csv.DictWriter(csv_buf, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow(
                    {k: ("" if v is None else _json_default(v) if not isinstance(v, (str, int, float)) else v)
                     for k, v in row.items()}
                )
            zf.writestr(f"csv/{table}.csv", csv_buf.getvalue())

    return buffer.getvalue()


def _finalize_export(db, export, *, zip_bytes: bytes, row_counts: dict[str, int]) -> None:
    """Write the bundle to storage and stamp the export row ready."""
    from datetime import timedelta

    storage_path = storage.write_bundle(export.organization_id, export.id, zip_bytes)
    export.storage_path = storage_path
    export.file_size_bytes = len(zip_bytes)
    export.row_counts_json = row_counts
    export.status = "ready"
    export.completed_at = datetime.now(timezone.utc)
    export.expires_at = datetime.now(timezone.utc) + timedelta(
        seconds=settings.tenant_export_link_ttl_seconds
    )


def run_export(db, export_id: int) -> dict[str, Any]:  # CRAFT-OK: dynamic export-result summary payload (pre-existing signature, unchanged by the RLS-scope wrap)
    """Synchronous core of the export task (callable directly in tests)."""
    # Resolve the owning organization from the exact export row under the vetted
    # bypass, then end that read transaction before changing RLS scope.
    with tenant_scope(bypass=True):
        export = db.query(models.TenantDataExport).filter(models.TenantDataExport.id == export_id).first()
        if export is None:
            raise ValueError(f"TenantDataExport {export_id} not found")
        organization_id = export.organization_id
        db.commit()

    with tenant_scope(org_id=organization_id):
        export = (
            db.query(models.TenantDataExport)
            .filter(
                models.TenantDataExport.id == export_id,
                models.TenantDataExport.organization_id == organization_id,
            )
            .one()
        )
        export.status = "processing"
        db.commit()

    try:
        # An organization export intentionally spans every authorized realm in
        # that organization. A single org/realm GUC pair cannot represent that
        # set, so this trusted operation uses the audited bypass while
        # serialize_organization/scoped_query retain explicit organization,
        # company, realm, and parent-link predicates for every model.
        with tenant_scope(bypass=True):
            export = (
                db.query(models.TenantDataExport)
                .filter(
                    models.TenantDataExport.id == export_id,
                    models.TenantDataExport.organization_id == organization_id,
                )
                .one()
            )
            serialized = serialize_organization(db, export.organization_id)
            zip_bytes = build_zip_bytes(export.organization_id, serialized)
            _finalize_export(db, export, zip_bytes=zip_bytes, row_counts=serialized["row_counts"])
            db.commit()
            return {"export_id": export_id, "status": export.status, "row_counts": serialized["row_counts"]}
    except Exception as exc:  # pragma: no cover - defensive
        db.rollback()
        with tenant_scope(org_id=organization_id):
            export = db.query(models.TenantDataExport).filter(models.TenantDataExport.id == export_id).first()
            if export is not None:
                export.status = "failed"
                export.error = redact_to_string(str(exc), max_length=1000)
                db.commit()
            logger.error(
                "[TenantExport] export %s failed: %s",
                export_id,
                redact_to_string(str(exc), max_length=500),
            )
        raise


@celery_app.task(bind=True, max_retries=2, name="app.tasks.data_export.export_tenant_data")
def export_tenant_data(self, export_id: int):
    """Celery entrypoint: serialize one org's data and store the bundle."""
    db = SessionLocal()
    try:
        return run_export(db, export_id)
    except storage.ExportStorageUnavailable:
        raise  # configuration, not a transient fault: the row is already "failed"
    except Exception as exc:
        raise self.retry(exc=exc, countdown=300)
    finally:
        db.close()

"""
app/services/tenant_export_storage.py
──────────────────────────────────────
Storage abstraction for tenant data-export bundles.

The bundle is built by the Celery worker and downloaded through the API
service, which run on separate machines with separate filesystems. So a bundle
is stored in the configured S3-compatible object storage
(``app/services/object_storage.py``) under
``tenants/<org>/exports/<file>.zip`` and handed out as a short-lived presigned
URL. Without object storage, a development/test environment (the ENVIRONMENT
values config.py treats as non-production) writes to the local directory
``settings.tenant_export_storage_dir``, which the worker and API must share:
docker-compose runs them as separate containers sharing only the ``/app`` bind
mount, so the default ``/tmp`` path is not shared there. Any other environment
refuses the export instead of writing a bundle no one can download.

Access is gated by an expiring, single-use signed link whose bearer secret is
never persisted: only its SHA-256 hash is stored on the ``TenantDataExport``
row, mirroring the ApprovalToken / AgentConfirmationToken pattern already used
in this codebase.
"""
from __future__ import annotations

import hashlib
import os
import secrets
from pathlib import Path
from typing import Optional

from ..config import settings
from . import object_storage

# ``TenantDataExport.storage_path`` prefix for a bundle held in object storage;
# anything else is a local development path.
OBJECT_STORAGE_PATH_PREFIX = "object-storage:"

# Matches config.py's development check (no production secrets required).
_LOCAL_DISK_ENVIRONMENTS = frozenset({"development", "dev", "test"})


class ExportStorageUnavailable(RuntimeError):
    """No durable, API-readable place to store an export bundle."""


def _storage_root() -> Path:
    root = Path(settings.tenant_export_storage_dir)
    root.mkdir(parents=True, exist_ok=True)
    return root


def _local_disk_allowed() -> bool:
    return settings.environment.lower() in _LOCAL_DISK_ENVIRONMENTS


def storage_ready() -> bool:
    """Whether an export bundle can be stored where the API can serve it."""
    return object_storage.is_configured() or _local_disk_allowed()


def hash_download_secret(secret: str) -> str:
    """Hash a download bearer secret for at-rest storage / lookup."""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def generate_download_secret() -> str:
    return secrets.token_urlsafe(32)


def bundle_filename(organization_id: int, export_id: int) -> str:
    return f"tenant_export_org_{organization_id}_{export_id}.zip"


def org_exports_prefix(organization_id: int) -> str:
    # Inside the tenant's ``tenants/<org>/`` prefix, which hard delete also purges.
    return f"tenants/{organization_id}/exports/"


def bundle_object_key(organization_id: int, export_id: int) -> str:
    return f"{org_exports_prefix(organization_id)}{bundle_filename(organization_id, export_id)}"


def is_object_storage_path(storage_path: Optional[str]) -> bool:
    return bool(storage_path) and storage_path.startswith(OBJECT_STORAGE_PATH_PREFIX)


def _object_key(storage_path: str) -> str:
    return storage_path[len(OBJECT_STORAGE_PATH_PREFIX):]


def write_bundle(organization_id: int, export_id: int, data: bytes) -> str:
    """Persist a zipped export bundle and return its storage path.

    Raises ``ExportStorageUnavailable`` when no store the API service can serve
    from is configured (a configuration fault: retrying cannot help), and a
    plain ``RuntimeError`` when the configured object store rejects the write
    (possibly transient: the export task retries it).
    """
    if object_storage.is_configured():
        key = bundle_object_key(organization_id, export_id)
        if object_storage.put_object(key, data, content_type="application/zip") != key:
            raise RuntimeError("Object storage did not accept the export bundle")
        return OBJECT_STORAGE_PATH_PREFIX + key
    if not _local_disk_allowed():
        raise ExportStorageUnavailable(
            "Tenant export storage is not configured: set the OBJECT_STORAGE_* settings"
        )
    root = _storage_root()
    path = root / bundle_filename(organization_id, export_id)
    # Write atomically so a partially-written bundle is never downloadable.
    tmp = path.with_suffix(".zip.partial")
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)
    return str(path)


def read_bundle(storage_path: str) -> Optional[bytes]:
    """Bytes of a LOCAL bundle; object-storage bundles are served by presigned URL."""
    if is_object_storage_path(storage_path):
        raise ValueError("Object-storage bundles are served by presigned URL, not read locally")
    path = Path(storage_path)
    if not path.is_file():
        return None
    with open(path, "rb") as fh:
        return fh.read()


def presigned_bundle_url(storage_path: str, filename: str) -> Optional[str]:
    """Short-lived presigned GET URL for an object-storage bundle, or None."""
    return object_storage.presigned_get_url(_object_key(storage_path), download_filename=filename)


def delete_bundle(storage_path: Optional[str]) -> bool:
    """Delete a stored bundle. Returns True if a bundle was removed."""
    if not storage_path:
        return False
    if is_object_storage_path(storage_path):
        return object_storage.delete_object(_object_key(storage_path))
    path = Path(storage_path)
    try:
        if path.is_file():
            path.unlink()
            return True
    except OSError:
        return False
    return False


def delete_org_bundles(organization_id: int) -> int:
    """Delete every export bundle belonging to an organization.

    Removes the org's object-storage export prefix (when configured) and any
    local bundles; returns the number of local files removed.
    """
    if object_storage.is_configured():
        object_storage.delete_prefix(org_exports_prefix(organization_id))
    root = Path(settings.tenant_export_storage_dir)
    if not root.is_dir():
        return 0
    removed = 0
    prefix = f"tenant_export_org_{organization_id}_"
    for child in root.glob(f"{prefix}*"):
        try:
            child.unlink()
            removed += 1
        except OSError:
            continue
    return removed


def signed_download_path(export_id: int, secret: str) -> str:
    """Relative API path for the expiring signed download link."""
    return f"/admin/tenants/exports/{export_id}/download?token={secret}"


def signed_download_url(export_id: int, secret: str) -> str:
    base = (settings.frontend_url or "").rstrip("/")
    return f"{base}{signed_download_path(export_id, secret)}"

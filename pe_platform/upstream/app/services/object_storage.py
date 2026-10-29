"""S3-compatible object storage for original uploaded documents and tenant
export bundles (``app/services/tenant_export_storage.py``).

Provider-agnostic: talks the S3 API against a configurable endpoint (Cloudflare
R2 by default; AWS S3 by swapping ``OBJECT_STORAGE_ENDPOINT_URL``/``_REGION``).

**Unconfigured-safe (load-bearing invariant).** When the endpoint/bucket/creds
are not set, every operation is a no-op that returns ``None``/``False`` and NEVER
raises — so a document upload still succeeds (text-only, no stored original) when
storage is off or misconfigured. Storage is an enhancement, never a hard
dependency of the upload path.

Security:
- ``boto3`` is imported lazily so a missing dependency/config can't break boot.
- Credentials come from settings and are NEVER logged (log-redaction rule); only
  object keys (which are tenant-prefixed, non-secret) appear in logs.
- The bucket is private; reads are handed out as short-lived presigned GET URLs.
"""
from __future__ import annotations

import logging
import threading
from typing import Optional

from ..config import settings

logger = logging.getLogger(__name__)

_CLIENT_LOCK = threading.Lock()
_CLIENT = None  # cached boto3 S3 client (opaque SDK object)
_CLIENT_SIGNATURE: Optional[tuple] = None


def is_configured() -> bool:
    """True only when endpoint + bucket + both credentials are present."""
    return bool(
        settings.object_storage_endpoint_url
        and settings.object_storage_bucket
        and settings.object_storage_access_key_id
        and settings.object_storage_secret_access_key
    )


def _client():
    """Lazily build + cache the boto3 S3 client, or None when unconfigured.

    Cached on the (endpoint, region, access-key) signature so a settings change
    in a long-lived process rebuilds rather than serving a stale client. Any
    construction error degrades to None (no-op), never propagates.
    """
    global _CLIENT, _CLIENT_SIGNATURE
    if not is_configured():
        return None
    signature = (
        settings.object_storage_endpoint_url,
        settings.object_storage_region,
        settings.object_storage_access_key_id,
    )
    if _CLIENT is not None and _CLIENT_SIGNATURE == signature:
        return _CLIENT
    with _CLIENT_LOCK:
        if _CLIENT is not None and _CLIENT_SIGNATURE == signature:
            return _CLIENT
        try:
            import boto3  # lazy: missing dep must not break boot
            from botocore.config import Config

            _CLIENT = boto3.client(
                "s3",
                endpoint_url=settings.object_storage_endpoint_url,
                region_name=settings.object_storage_region or "auto",
                aws_access_key_id=settings.object_storage_access_key_id,
                aws_secret_access_key=settings.object_storage_secret_access_key,
                config=Config(
                    signature_version="s3v4",
                    connect_timeout=5,
                    read_timeout=30,
                    retries={"max_attempts": 3, "mode": "standard"},
                ),
            )
            _CLIENT_SIGNATURE = signature
            return _CLIENT
        except Exception:  # pragma: no cover - construction failure degrades to no-op
            logger.exception("object_storage: client construction failed; storage disabled this call")
            return None


def put_object(key: str, data: bytes, content_type: Optional[str] = None) -> Optional[str]:
    """Store ``data`` under ``key``. Returns the key on success, else None.

    No-op (returns None) when unconfigured or on any error — the caller must
    treat None as "no original stored" and continue (text-only document).
    """
    client = _client()
    if client is None:
        return None
    try:
        extra = {"ContentType": content_type} if content_type else {}
        client.put_object(
            Bucket=settings.object_storage_bucket,
            Key=key,
            Body=data,
            **extra,
        )
        return key
    except Exception:
        # Do NOT include credentials; key is tenant-prefixed + non-secret.
        logger.exception("object_storage: put_object failed for key=%s", key)
        return None


def presigned_get_url(
    key: str,
    ttl: Optional[int] = None,
    *,
    download_filename: Optional[str] = None,
) -> Optional[str]:
    """Return a short-lived presigned GET URL for ``key``, or None.

    The caller MUST enforce tenant access BEFORE calling this — a presigned URL
    grants anyone who holds it read access until it expires. ``download_filename``
    makes the response an attachment with that name.
    """
    client = _client()
    if client is None:
        return None
    expires = ttl if ttl is not None else settings.object_storage_presign_ttl_seconds
    params = {"Bucket": settings.object_storage_bucket, "Key": key}
    if download_filename:
        params["ResponseContentDisposition"] = f'attachment; filename="{download_filename}"'
    try:
        return client.generate_presigned_url(
            "get_object",
            Params=params,
            ExpiresIn=int(expires),
        )
    except Exception:
        logger.exception("object_storage: presign failed for key=%s", key)
        return None


def delete_object(key: str) -> bool:
    """Delete one object. Idempotent; False when unconfigured or on any error."""
    client = _client()
    if client is None:
        return False
    try:
        client.delete_object(Bucket=settings.object_storage_bucket, Key=key)
        return True
    except Exception:
        logger.exception("object_storage: delete_object failed for key=%s", key)
        return False


def delete_prefix(prefix: str) -> bool:
    """Delete every object under ``prefix`` (e.g. a tenant's documents).

    Idempotent: missing objects/prefix is success. No-op (returns False) when
    unconfigured. Used by tenant hard-delete (GDPR) — must purge originals with
    the tenant's rows.
    """
    client = _client()
    if client is None:
        return False
    if not prefix:
        # Refuse an empty prefix — would target the whole bucket.
        logger.error("object_storage: delete_prefix refused empty prefix")
        return False
    bucket = settings.object_storage_bucket
    try:
        paginator = client.get_paginator("list_objects_v2")
        to_delete: list[dict] = []
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []) or []:
                to_delete.append({"Key": obj["Key"]})
                if len(to_delete) == 1000:  # S3 delete_objects caps at 1000/batch
                    client.delete_objects(Bucket=bucket, Delete={"Objects": to_delete})
                    to_delete = []
        if to_delete:
            client.delete_objects(Bucket=bucket, Delete={"Objects": to_delete})
        return True
    except Exception:
        logger.exception("object_storage: delete_prefix failed for prefix=%s", prefix)
        return False

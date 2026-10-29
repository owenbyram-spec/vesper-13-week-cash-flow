from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any, Iterable, Protocol
from urllib.parse import quote

from fastapi import HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.orm import Session

from ..config import is_dev_environment, settings
from ..models import AuditEntry, User
from .redaction import redact_to_string

logger = logging.getLogger(__name__)


DOCUMENT_UPLOAD_CONTENT_TYPES = frozenset(
    {
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/msword",
        "text/plain",
        "application/octet-stream",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "text/csv",
    }
)
DOCUMENT_UPLOAD_EXTENSIONS = frozenset({"pdf", "docx", "doc", "txt", "xlsx", "csv"})

AGREEMENT_UPLOAD_CONTENT_TYPES = frozenset(
    {
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/msword",
        "text/plain",
        "application/octet-stream",
    }
)
AGREEMENT_UPLOAD_EXTENSIONS = frozenset({"pdf", "docx", "doc", "txt"})

SPREADSHEET_UPLOAD_CONTENT_TYPES = frozenset(
    {
        "text/csv",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.ms-excel",
        "application/octet-stream",
    }
)
SPREADSHEET_UPLOAD_EXTENSIONS = frozenset({"csv", "xlsx", "xls"})


@dataclass(frozen=True)
class MalwareScanResult:
    status: str
    provider: str
    signature: str | None = None
    details: dict[str, Any] | None = None


@dataclass(frozen=True)
class ValidatedUpload:
    filename: str
    content_type: str
    extension: str
    size_bytes: int
    content: bytes
    scan_status: str = "skipped"
    scan_provider: str = "disabled"
    scan_result: dict[str, Any] | None = None


class MalwareScanner(Protocol):
    provider: str

    def scan(self, *, content: bytes, filename: str, content_type: str, purpose: str) -> MalwareScanResult:
        ...


class HttpMalwareScanner:
    provider = "http"

    def __init__(self, *, url: str, timeout_seconds: float = 10.0):
        self.url = url
        self.timeout_seconds = timeout_seconds

    def scan(self, *, content: bytes, filename: str, content_type: str, purpose: str) -> MalwareScanResult:
        request = urllib.request.Request(
            self.url,
            data=content,
            method="POST",
            headers={
                "content-type": content_type or "application/octet-stream",
                # http.client encodes header values as Latin-1, so a raw name with a
                # curly apostrophe, CJK or emoji raised before the request was sent and
                # the upload failed closed. Percent-encoded UTF-8 is always ASCII-safe;
                # the scanner decodes it for logging only.
                "x-upload-filename": quote(filename, safe=""),
                "x-upload-purpose": purpose,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                body = response.read()
                if response.status in {204, 205}:
                    return MalwareScanResult(status="clean", provider=self.provider)
                payload = json.loads(body.decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            if exc.code in {406, 422}:
                try:
                    payload = json.loads(exc.read().decode("utf-8") or "{}")
                except Exception:
                    payload = {}
                return MalwareScanResult(
                    status="infected",
                    provider=self.provider,
                    signature=str(payload.get("signature") or payload.get("threat") or "malware"),
                    details={"http_status": exc.code, "scanner": payload},
                )
            return MalwareScanResult(
                status="error",
                provider=self.provider,
                details={"error": f"scanner_http_{exc.code}"},
            )
        except Exception as exc:
            return MalwareScanResult(
                status="error",
                provider=self.provider,
                details={"error": redact_to_string(str(exc), max_length=500)},
            )

        status = str(payload.get("status") or payload.get("result") or "").strip().lower()
        if status in {"clean", "ok", "passed"}:
            return MalwareScanResult(status="clean", provider=self.provider, details=payload)
        if status in {"infected", "malicious", "virus", "found", "failed"}:
            return MalwareScanResult(
                status="infected",
                provider=self.provider,
                signature=str(payload.get("signature") or payload.get("threat") or "malware"),
                details=payload,
            )
        return MalwareScanResult(status="error", provider=self.provider, details={"scanner": payload})


class EicarTestScanner:
    provider = "eicar_test"

    def scan(self, *, content: bytes, filename: str, content_type: str, purpose: str) -> MalwareScanResult:
        if b"EICAR-STANDARD-ANTIVIRUS-TEST-FILE" in content:
            return MalwareScanResult(status="infected", provider=self.provider, signature="EICAR-Test-File")
        return MalwareScanResult(status="clean", provider=self.provider)


def _normalize_content_type(content_type: str | None) -> str:
    return (content_type or "").split(";", 1)[0].strip().lower()


def _extension_for(filename: str) -> str:
    if "." not in filename:
        return ""
    return filename.rsplit(".", 1)[-1].strip().lower()


def _safe_upload_filename(filename: str | None) -> str:
    return os.path.basename(filename or "upload") or "upload"


def _json_safe(value: Any) -> Any:
    try:
        json.dumps(value)
        return value
    except TypeError:
        return str(value)


def _content_type_matches_extension(content_type: str, extension: str) -> bool:
    if not content_type or content_type == "application/octet-stream":
        return True
    allowed_extensions = {
        "application/pdf": {"pdf"},
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": {"docx"},
        "application/msword": {"doc"},
        "text/plain": {"txt", "csv"},
        "text/csv": {"csv"},
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": {"xlsx"},
        "application/vnd.ms-excel": {"xls", "csv"},
    }.get(content_type)
    return allowed_extensions is None or extension in allowed_extensions


def _looks_like_allowed_file(content: bytes, extension: str) -> bool:
    sample = content[:4096]
    if extension == "pdf":
        return content.startswith(b"%PDF")
    if extension in {"docx", "xlsx"}:
        return content.startswith(b"PK\x03\x04") or content.startswith(b"PK\x05\x06") or content.startswith(b"PK\x07\x08")
    if extension in {"doc", "xls"}:
        return content.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")
    if extension in {"txt", "csv"}:
        return b"\x00" not in sample
    return False


def _is_production_like() -> bool:
    return not is_dev_environment(os.getenv("ENVIRONMENT", settings.environment).strip())


def _setting_or_env(env_name: str, configured_value: str | float) -> str:
    value = os.getenv(env_name)
    if value is None:
        value = str(configured_value)
    return value.strip()


def _scanner_mode_from_config() -> str:
    mode = _setting_or_env("UPLOAD_MALWARE_SCANNER_MODE", settings.upload_malware_scanner_mode).lower()
    if not mode:
        return "required" if _is_production_like() else "disabled"
    return mode


def _scanner_url_from_config() -> str:
    url = _setting_or_env(
        "UPLOAD_MALWARE_SCANNER_URL",
        os.getenv("UPLOAD_MALWARE_SCANNER") or settings.upload_malware_scanner_url,
    )
    if url:
        if "://" not in url:
            scheme = _setting_or_env(
                "UPLOAD_MALWARE_SCANNER_SCHEME",
                settings.upload_malware_scanner_scheme,
            ).lower()
            return f"{scheme}://{url}"
        return url
    hostport = _setting_or_env(
        "UPLOAD_MALWARE_SCANNER_HOSTPORT",
        settings.upload_malware_scanner_hostport,
    )
    if not hostport:
        return ""
    scheme = _setting_or_env(
        "UPLOAD_MALWARE_SCANNER_SCHEME",
        settings.upload_malware_scanner_scheme,
    ).lower()
    path = _setting_or_env(
        "UPLOAD_MALWARE_SCANNER_PATH",
        settings.upload_malware_scanner_path,
    )
    if not path.startswith("/"):
        path = f"/{path}"
    return f"{scheme}://{hostport}{path}"


def _scanner_from_env() -> MalwareScanner | None:
    provider = _setting_or_env(
        "UPLOAD_MALWARE_SCANNER_PROVIDER",
        settings.upload_malware_scanner_provider,
    ).lower()
    if not provider:
        return None
    if provider in {"http", "clamav_http", "external_http"}:
        url = _scanner_url_from_config()
        if not url:
            raise HTTPException(status_code=500, detail="Upload malware scanner URL is not configured.")
        timeout = float(
            _setting_or_env(
                "UPLOAD_MALWARE_SCANNER_TIMEOUT_SECONDS",
                settings.upload_malware_scanner_timeout_seconds,
            )
        )
        return HttpMalwareScanner(url=url, timeout_seconds=timeout)
    if provider in {"eicar_test", "test"}:
        if _is_production_like():
            raise HTTPException(status_code=500, detail="Test malware scanner provider is not allowed in production.")
        return EicarTestScanner()
    raise HTTPException(status_code=500, detail="Invalid upload scanner provider.")


def scan_upload_bytes(*, content: bytes, filename: str, content_type: str, purpose: str) -> MalwareScanResult:
    """Scan upload content and fail closed when configured as required."""
    mode = _scanner_mode_from_config()
    if mode in {"", "disabled", "off", "false"}:
        return MalwareScanResult(status="skipped", provider="disabled")
    if mode not in {"required", "enforce", "fail_closed", "log_only", "monitor"}:
        raise HTTPException(status_code=500, detail="Invalid upload scanner mode.")

    scanner = _scanner_from_env()
    if scanner is None:
        if mode in {"log_only", "monitor"}:
            logger.warning("Upload scanner monitor mode enabled without a configured provider.")
            return MalwareScanResult(status="skipped", provider="unconfigured")
        raise HTTPException(
            status_code=503,
            detail="Upload malware scanner is required but not configured.",
        )

    result = scanner.scan(content=content, filename=filename, content_type=content_type, purpose=purpose)
    if result.status == "clean":
        return result
    if result.status == "infected":
        logger.warning(
            "Upload quarantined filename=%s purpose=%s scanner=%s signature=%s",
            filename,
            purpose,
            result.provider,
            result.signature,
        )
        if mode in {"log_only", "monitor"}:
            return result
        raise HTTPException(
            status_code=422,
            detail={
                "code": "upload_quarantined",
                "message": "Upload quarantined by malware scanner.",
                "scan_status": result.status,
                "scan_provider": result.provider,
                "signature": result.signature,
            },
        )
    logger.warning("Upload scanner failed filename=%s purpose=%s scanner=%s", filename, purpose, result.provider)
    if mode in {"log_only", "monitor"}:
        return result
    raise HTTPException(
        status_code=503,
        detail={
            "code": "upload_scan_failed",
            "message": "Upload malware scanner failed closed.",
            "scan_status": "error",
            "scan_provider": result.provider,
        },
    )


def record_upload_audit_event(
    db: Session,
    *,
    company_id: str,
    user: User,
    action: str,
    purpose: str,
    filename: str | None,
    entity_type: str | None = None,
    entity_id: str | None = None,
    status_code: int | None = None,
    scan_result: dict[str, Any] | None = None,
    detail: Any = None,
) -> None:
    details: dict[str, Any] = {
        "purpose": purpose,
        "filename": _safe_upload_filename(filename),
    }
    if status_code is not None:
        details["status_code"] = status_code
    if scan_result:
        details["scan_status"] = scan_result.get("status")
        details["scan_provider"] = scan_result.get("provider")
        if scan_result.get("signature"):
            details["signature"] = scan_result.get("signature")
    if isinstance(detail, dict):
        for key in ("code", "scan_status", "scan_provider", "signature"):
            if detail.get(key) is not None and key not in details:
                details[key] = detail.get(key)
    if detail is not None:
        details["detail"] = _json_safe(detail)

    db.add(
        AuditEntry(
            company_id=str(company_id),
            user_id=str(user.id) if user and user.id is not None else None,
            user_email=user.email if user else None,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            details=details,
        )
    )


async def validate_upload_file(
    file: UploadFile,
    *,
    allowed_content_types: Iterable[str],
    allowed_extensions: Iterable[str],
    max_size_mb: int,
    purpose: str,
) -> ValidatedUpload:
    filename = _safe_upload_filename(file.filename)
    content_type = _normalize_content_type(file.content_type)
    allowed_types = {item.lower() for item in allowed_content_types}
    allowed_exts = {item.lower().lstrip(".") for item in allowed_extensions}
    extension = _extension_for(filename)

    if content_type and content_type not in allowed_types:
        raise HTTPException(status_code=415, detail=f"Unsupported file type: {content_type}")
    if not extension or extension not in allowed_exts:
        raise HTTPException(status_code=415, detail=f"Unsupported file extension: {extension or 'none'}")
    if not _content_type_matches_extension(content_type, extension):
        raise HTTPException(
            status_code=415,
            detail=f"File extension .{extension} does not match content type {content_type}",
        )

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")

    size_bytes = len(content)
    max_bytes = max_size_mb * 1_000_000
    if size_bytes > max_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"File too large ({size_bytes / 1_000_000:.1f}MB). Max {max_size_mb}MB.",
        )

    if not _looks_like_allowed_file(content, extension):
        raise HTTPException(status_code=415, detail="Uploaded file content does not match the declared file type.")

    # ``scan_upload_bytes`` reaches the configured scanner through the synchronous
    # ``urllib`` client.  ``validate_upload_file`` is awaited by async upload
    # handlers, so invoking that client inline would stall the event loop for the
    # full scanner timeout.  Keep the shared scanner contract synchronous for its
    # existing sync callers, but always offload it at this async boundary.
    scan_result = await run_in_threadpool(
        scan_upload_bytes,
        content=content,
        filename=filename,
        content_type=content_type,
        purpose=purpose,
    )

    return ValidatedUpload(
        filename=filename,
        content_type=content_type,
        extension=extension,
        size_bytes=size_bytes,
        content=content,
        scan_status=scan_result.status,
        scan_provider=scan_result.provider,
        scan_result={
            "status": scan_result.status,
            "provider": scan_result.provider,
            **({"signature": scan_result.signature} if scan_result.signature else {}),
            **({"details": scan_result.details} if scan_result.details else {}),
        },
    )

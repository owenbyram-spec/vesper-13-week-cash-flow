"""Celery task that drives verify-on-ingest of the legal-versions manifest (SPEC §6.9).

The ingest worker mirrors the CI-published ``manifest.json`` into the single
``legal_document_versions`` table, re-hashing each R2 snapshot before trusting it
(``app/services/legal_manifest.verify_and_ingest``). A rejected entry (bytes disagree
with the manifest) is NOT mirrored — the prior version is retained and an alert fires.

Reliability contract (celery-redis-jobs skill):
- **acks_late + task_reject_on_worker_lost + JSON serializer** are inherited from the app
  config (``app/celery_app.py``) — this task does not re-declare them. So a worker killed
  mid-ingest re-runs the task rather than silently dropping it (at-least-once delivery).
- **Idempotent under redelivery:** ``verify_and_ingest`` upserts by the immutable
  ``(document_type, version)`` key, so re-running the task re-verifies and updates the same
  rows — it never inserts a duplicate. That is what makes at-least-once delivery safe.
- **Own SessionLocal in try/finally** — a worker has no request-scoped ``get_db``.
- **Bounded exponential-backoff retry** (max_retries=3) on a transient fetch/DB error; a
  verify MISMATCH is not an exception (it returns a rejected result), so a tampered
  snapshot does NOT burn the retry budget. A deterministic ``ValueError`` (no manifest
  source) escapes without retry — a poison payload is surfaced, not looped.

**Ops config (DEFERRED — out of this wave's scope):** the live trigger is the marketing
repo's ``legal.version.published`` webhook plus a 5-minute safety poll of ``manifest.json``
with ``If-None-Match`` (SPEC §6.9 step 2). Wiring the R2/CDN manifest URL and the beat/webhook
trigger is ops configuration; this module ships the task + the manifest-source seam only and
declares NO beat schedule.
"""
import json
import logging

from ..celery_app import celery_app
from ..db import SessionLocal
from ..services.legal_manifest import (
    _default_fetch,
    iter_manifest_entries,
    verify_and_ingest,
)
from ..services.redaction import redact_to_string

logger = logging.getLogger(__name__)


def _load_manifest(manifest: dict | None, manifest_uri: str | None) -> dict:
    """Resolve the manifest document from the injected source (config-driven).

    Prefers an already-loaded ``manifest`` dict (the caller/webhook payload); otherwise
    fetches + JSON-parses ``manifest_uri`` through the same boundary fetch the snapshot
    re-hash uses. Raises ``ValueError`` (a deterministic, non-retryable error) when neither
    source is supplied — there is nothing to ingest.
    """
    if manifest is not None:
        return manifest
    if manifest_uri:
        return json.loads(_default_fetch(manifest_uri).decode("utf-8"))
    raise ValueError("ingest_legal_manifest: no manifest source (pass manifest or manifest_uri)")


@celery_app.task(
    bind=True,
    max_retries=3,
    name="app.tasks.legal_ingest.ingest_legal_manifest",
)
def ingest_legal_manifest(self, manifest: dict | None = None, manifest_uri: str | None = None) -> dict:
    """Verify + mirror every version in the manifest into ``legal_document_versions``.

    Idempotent + safe to retry: each entry is verified-on-ingest and upserted by
    (document_type, version). Returns a summary ``{status, entries, ingested, rejected}``.
    """
    db = SessionLocal()
    try:
        manifest_doc = _load_manifest(manifest, manifest_uri)
        entries = iter_manifest_entries(manifest_doc)

        ingested = 0
        rejected = 0
        for entry in entries:
            # IDEMPOTENT-OK: verify_and_ingest upserts by the immutable (document_type, version)
            # key — a redelivery re-verifies + updates the same single row, never duplicating.
            result = verify_and_ingest(db, entry)
            if result.verified:
                ingested += 1
            else:
                rejected += 1

        logger.info(
            "[legal_ingest] manifest ingest complete: entries=%s ingested=%s rejected=%s",
            len(entries),
            ingested,
            rejected,
        )
        return {
            "status": "ok",
            "entries": len(entries),
            "ingested": ingested,
            "rejected": rejected,
        }
    except ValueError:
        # Deterministic input error (no manifest source) — surface it loudly; a poison
        # payload must NOT be retried into a loop (bounded budget aside, it never self-heals).
        db.rollback()
        raise
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        logger.error(
            "[legal_ingest] manifest ingest failed: %s",
            redact_to_string(str(exc), max_length=500),
        )
        # Capped exponential backoff (AP-CEL-4): 60s, 120s, 240s bounded at 600s.
        raise self.retry(exc=exc, countdown=min(60 * (2 ** self.request.retries), 600)) from exc
    finally:
        db.close()

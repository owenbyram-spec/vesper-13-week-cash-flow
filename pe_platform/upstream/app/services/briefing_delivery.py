"""Briefing-only daily delivery ledger; SMTP uncertainty never authorizes resend."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone, timedelta

from sqlalchemy.exc import IntegrityError

from ..models import BriefingDelivery, OrganizationCompany


def _digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, default=str).encode()
    ).hexdigest()


def _payload(row, *, reused=False):
    return {
        "status": "already_sent" if reused and row.status == "sent" else row.status,
        "delivery_id": row.id,
        "reason": row.reason,
        "attempts": row.attempts,
    }


def deliver_briefing(db, *, company_id, briefing, smtp, send):
    company = (
        db.query(OrganizationCompany)
        .filter(OrganizationCompany.realm_id == company_id)
        .first()
    )
    if company is None or company.status != "active":
        raise PermissionError("briefing_scope_unavailable")
    if (
        briefing.company_id != company_id
        or briefing.organization_id not in (None, company.organization_id)
        or briefing.organization_company_id not in (None, company.id)
    ):
        raise PermissionError("briefing_scope_mismatch")
    query = db.query(BriefingDelivery).filter(
        BriefingDelivery.organization_id == company.organization_id,
        BriefingDelivery.organization_company_id == company.id,
        BriefingDelivery.company_id == company_id,
        BriefingDelivery.delivery_date == briefing.briefing_date,
        BriefingDelivery.kind == "morning_email",
    )
    row = query.populate_existing().with_for_update().first()
    if row is None:
        identity = str(uuid.uuid4())
        row = BriefingDelivery(
            id=identity,
            organization_id=company.organization_id,
            organization_company_id=company.id,
            company_id=company_id,
            briefing_id=briefing.id,
            delivery_date=briefing.briefing_date,
            kind="morning_email",
            content_digest=_digest(
                {
                    "financial_position": briefing.financial_position,
                    "key_risks": briefing.key_risks,
                    "narrative": briefing.narrative,
                }
            ),
            recipient_digest=_digest(
                sorted(
                    {
                        r.strip().lower()
                        for r in smtp["recipient"].split(",")
                        if r.strip()
                    }
                )
            ),
            message_id=f"<{identity}@briefing.byram.invalid>",
            status="ready",
            attempts=0,
            revision=1,
        )
        db.add(row)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
        row = query.populate_existing().with_for_update().one()
    now = datetime.now(timezone.utc)
    if row.status == "sending":
        started = (
            row.started_at.replace(tzinfo=timezone.utc)
            if row.started_at and row.started_at.tzinfo is None
            else row.started_at
        )
        if started is None or started < now - timedelta(minutes=30):
            row.status = "uncertain"
            row.reason = "sending_completion_unknown"
            db.commit()
        else:
            db.commit()
        return _payload(row, reused=True)
    if row.status not in ("ready", "retryable") or row.attempts >= 3:
        if row.status == "retryable":
            row.status, row.reason = "failed", "pre_data_retry_limit"
        db.commit()
        return _payload(row, reused=True)
    token, revision, delivery_id = str(uuid.uuid4()), row.revision, row.id
    changed = query.filter(
        BriefingDelivery.status.in_(("ready", "retryable")),
        BriefingDelivery.revision == revision,
        BriefingDelivery.attempts < 3,
    ).update(
        {
            "status": "sending",
            "claim_token": token,
            "started_at": now,
            "attempts": row.attempts + 1,
            "revision": revision + 1,
        },
        synchronize_session=False,
    )
    if changed != 1:
        db.rollback()
        return _payload(query.populate_existing().one(), reused=True)
    message_id = row.message_id
    db.commit()  # If this fails, send is never entered.
    try:
        result = send(message_id)
        state, reason = result.status, result.reason
        if state not in ("sent", "partial", "retryable", "failed", "uncertain"):
            state, reason = "uncertain", "unrecognized_transport_result"
    except Exception:
        state, reason = "uncertain", "transport_completion_unknown"
    try:
        row = (
            query.filter(BriefingDelivery.id == delivery_id)
            .populate_existing()
            .with_for_update()
            .one()
        )
        if row.claim_token != token or row.revision != revision + 1:
            db.rollback()
            return {
                "status": "uncertain",
                "delivery_id": delivery_id,
                "reason": "delivery_claim_lost",
            }
        row.status, row.reason = state, reason
        if state == "retryable" and row.attempts >= 3:
            row.status, row.reason = "failed", "pre_data_retry_limit"
        row.finished_at = now if row.status in ("sent", "partial", "failed") else None
        row.revision += 1
        db.commit()
        return _payload(row)
    except Exception:
        db.rollback()
        # Persisted sending claim blocks all repeats even if this update failed.
        return {
            "status": "uncertain",
            "delivery_id": delivery_id,
            "reason": "delivery_receipt_persistence_unknown",
        }

"""Per-tenant confidence calibration (Autonomy roadmap, Phase 2 PR1).

A model's raw ``confidence_score`` is not the same as the probability a human
will actually agree with it — and that gap differs per tenant. This module
builds, from a tenant's own ``AICorrectionEvent`` history, an empirical
reliability curve: for predictions the model scored in a given raw-confidence
bucket, how often did the human accept the AI's output as-is? ``calibrated_
confidence`` then maps a new raw score to that tenant's observed agreement rate.

Strictly per-tenant — calibration NEVER pools across tenants. Cold-start: with
too few samples (per bucket or overall) the result is flagged ``uncalibrated``
and must never be treated as high-confidence by the gate.

Reads ``AICorrectionEvent`` only; no model calls.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy.orm import Session

from ..config import settings
from ..models import AICorrectionEvent
from ..tenant_rls import org_grain_read

# A human "agreed" with the AI when they took its output as-is.
_AGREEING_DECISIONS = {"accepted", "confirmed"}


def _bucket_index(raw_score: float, buckets: int) -> int:
    """Equal-width bucket index in ``[0, buckets)`` for a raw score in [0, 1]."""
    if raw_score < 0:
        raw_score = 0.0
    if raw_score >= 1:
        return buckets - 1
    return min(buckets - 1, int(raw_score * buckets))


def build_calibration_map(
    db: Session,
    *,
    organization_id: int,
    task_type: str = "categorization",
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    organization_company_id: Optional[int] = None,
) -> dict[str, Any]:
    """Build a tenant's reliability curve over the optional window.

    Returns ``{buckets: [{index, lo, hi, count, agreement_rate}], total}`` where
    ``agreement_rate`` is the empirical fraction of in-bucket rows the human
    accepted as-is. Only rows with a non-NULL ``ai_confidence`` count.
    """
    n_buckets = max(1, settings.autonomy_calibration_buckets)
    filters = [
        AICorrectionEvent.organization_id == organization_id,
        AICorrectionEvent.task_type == task_type,
        AICorrectionEvent.ai_confidence.isnot(None),
    ]
    if organization_company_id is not None:
        filters.append(AICorrectionEvent.organization_company_id == organization_company_id)
    if start is not None:
        filters.append(AICorrectionEvent.created_at >= start)
    if end is not None:
        filters.append(AICorrectionEvent.created_at < end)

    # E-3: org-grain calibration aggregate. It projects ONLY ai_confidence and
    # human_decision into bucket counts and an agreement rate - never row content - and is
    # keyed on organization_id exactly like every other input to the same autonomy
    # decision (effective_level, effective_threshold, autoexec_count_today).
    with org_grain_read(
        "org-grain calibration: projects only confidence/decision into bucket counts, "
        "matching the org keying of every other input to the autonomy decision"
    ):
        rows = db.query(
            AICorrectionEvent.ai_confidence,
            AICorrectionEvent.human_decision,
        ).filter(*filters).all()

    agreed = [0] * n_buckets
    total = [0] * n_buckets
    for raw, decision in rows:
        idx = _bucket_index(float(raw), n_buckets)
        total[idx] += 1
        if decision in _AGREEING_DECISIONS:
            agreed[idx] += 1

    width = 1.0 / n_buckets
    buckets = [
        {
            "index": i,
            "lo": round(i * width, 6),
            "hi": round((i + 1) * width, 6),
            "count": total[i],
            "agreement_rate": (agreed[i] / total[i]) if total[i] else None,
        }
        for i in range(n_buckets)
    ]
    return {
        "organization_id": organization_id,
        "task_type": task_type,
        "buckets": buckets,
        "total": sum(total),
    }


def calibrated_confidence(
    db: Session,
    *,
    organization_id: int,
    raw_score: Optional[float],
    task_type: str = "categorization",
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    organization_company_id: Optional[int] = None,
) -> dict[str, Any]:
    """Map a raw confidence score to the tenant's observed agreement rate.

    Returns ``{calibrated, uncalibrated, bucket_index, bucket_samples, total_
    samples}``. ``calibrated`` is the empirical agreement rate for the raw score's
    bucket, or ``None`` when uncalibrated. ``uncalibrated`` is True (and
    ``calibrated`` must NOT be treated as high-confidence) when the raw score is
    missing, the tenant has fewer than the configured total samples, or the
    score's bucket has fewer than the configured per-bucket samples.
    """
    if raw_score is None:
        return {
            "calibrated": None,
            "uncalibrated": True,
            "reason": "no_raw_score",
            "bucket_index": None,
            "bucket_samples": 0,
            "total_samples": 0,
        }

    cal_map = build_calibration_map(
        db,
        organization_id=organization_id,
        task_type=task_type,
        start=start,
        end=end,
        organization_company_id=organization_company_id,
    )
    total_samples = cal_map["total"]
    n_buckets = len(cal_map["buckets"])
    idx = _bucket_index(float(raw_score), n_buckets)
    bucket = cal_map["buckets"][idx]
    bucket_samples = bucket["count"]

    if total_samples < settings.autonomy_calibration_min_total_samples:
        reason = "cold_start_total"
    elif bucket_samples < settings.autonomy_calibration_min_bucket_samples:
        reason = "cold_start_bucket"
    else:
        reason = None

    uncalibrated = reason is not None
    return {
        "calibrated": None if uncalibrated else bucket["agreement_rate"],
        "uncalibrated": uncalibrated,
        "reason": reason,
        "bucket_index": idx,
        "bucket_samples": bucket_samples,
        "total_samples": total_samples,
    }


def tenant_threshold(task_type: str = "categorization") -> float:
    """The confidence threshold a calibrated score must meet to be would-be-auto.

    Default-only here (PR1). PR2's TenantTaskAutonomy.confidence_threshold
    overrides per tenant; the gate passes that in explicitly when set.
    """
    return settings.autonomy_categorization_confidence_threshold


def evaluate_shadow(
    db: Session,
    *,
    organization_id: int,
    task_type: str,
    raw_confidence: Optional[float],
    human_decision: str,
    threshold: Optional[float] = None,
    organization_company_id: Optional[int] = None,
) -> dict[str, Any]:
    """Shadow-evaluate one prediction at human-decision time.

    Computes ``would_autoexecute`` (calibrated confidence is calibrated AND meets
    the threshold) and ``matched_human`` (the human accepted the AI output as-is).
    Uncalibrated scores are NEVER would-be-auto. Returns the booleans plus the
    calibration detail. Best-effort callers should treat any exception as "not
    evaluated" (None/None).
    """
    cal = calibrated_confidence(
        db,
        organization_id=organization_id,
        raw_score=raw_confidence,
        task_type=task_type,
        organization_company_id=organization_company_id,
    )
    thr = threshold if threshold is not None else tenant_threshold(task_type)
    would_autoexecute = (not cal["uncalibrated"]) and (cal["calibrated"] is not None) and (cal["calibrated"] >= thr)
    matched_human = human_decision in _AGREEING_DECISIONS
    return {
        "would_autoexecute": bool(would_autoexecute),
        "matched_human": bool(matched_human),
        "calibrated": cal["calibrated"],
        "uncalibrated": cal["uncalibrated"],
        "threshold": thr,
    }

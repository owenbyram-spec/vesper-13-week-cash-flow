"""Per-task per-tenant agreement scoring over a window (read surface).

Rolls up the ``AICorrectionEvent`` rows written by ``correction_capture`` into a
per-task agreement summary for one tenant. This is the substrate the autonomy
roadmap uses to decide whether a task has earned promotion: a high accepted /
agreement rate over enough samples is the signal that the AI's output for that
task, for that tenant, is trustworthy.

Reads ``AICorrectionEvent`` only — never calls a model. Realm-isolated: every
query is scoped to one ``organization_id`` (and optionally one company); a tenant
never sees another tenant's rows.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy.orm import Session

from ..config import settings
from ..models import AICorrectionEvent

# Decisions that mean the human agreed with the AI as-is.
_AGREEING_DECISIONS = {"accepted", "confirmed"}


def default_window(days: int = 30) -> tuple[datetime, datetime]:
    """A trailing ``[now - days, now)`` window in UTC."""
    now = datetime.now(timezone.utc)
    return now - timedelta(days=days), now


def task_agreement_summary(
    db: Session,
    *,
    organization_id: int,
    task_type: str,
    start: datetime,
    end: datetime,
    organization_company_id: Optional[int] = None,
) -> dict[str, Any]:
    """Agreement metrics for one tenant + task_type over ``[start, end)``.

    Returns per-decision rates (accepted / edited / rejected / dismissed /
    confirmed), the raw decision counts, a headline ``agreement_rate``
    (accepted + confirmed over total — the task-agnostic trust signal), and a
    structural ``exact_match_rate`` over the subset of rows where ``ai_output``
    and ``human_output`` are directly comparable (same dict shape).

    Reads only this org's rows — cross-tenant isolation is enforced by the
    ``organization_id`` filter.
    """
    filters = [
        AICorrectionEvent.organization_id == organization_id,
        AICorrectionEvent.task_type == task_type,
        AICorrectionEvent.created_at >= start,
        AICorrectionEvent.created_at < end,
    ]
    if organization_company_id is not None:
        filters.append(AICorrectionEvent.organization_company_id == organization_company_id)

    rows = db.query(AICorrectionEvent).filter(*filters).all()
    count = len(rows)

    tally = Counter(r.human_decision for r in rows)

    def rate(decision: str) -> float:
        return (tally.get(decision, 0) / count) if count else 0.0

    agreeing = sum(tally.get(d, 0) for d in _AGREEING_DECISIONS)

    # Structural exact-match only where both sides are dicts (comparable).
    comparable = [
        r for r in rows
        if isinstance(r.ai_output, dict) and isinstance(r.human_output, dict)
    ]
    exact_matches = sum(1 for r in comparable if r.ai_output == r.human_output)

    return {
        "organization_id": organization_id,
        "organization_company_id": organization_company_id,
        "task_type": task_type,
        "start": start,
        "end": end,
        "count": count,
        "accepted_rate": rate("accepted"),
        "edited_rate": rate("edited"),
        "rejected_rate": rate("rejected"),
        "dismissed_rate": rate("dismissed"),
        "confirmed_rate": rate("confirmed"),
        "agreement_rate": (agreeing / count) if count else None,
        "exact_match_rate": (exact_matches / len(comparable)) if comparable else None,
        "exact_match_sample": len(comparable),
        "decision_counts": dict(tally),
    }


def promotion_gate_summary(
    db: Session,
    *,
    organization_id: int,
    task_type: str,
    start: datetime,
    end: datetime,
    organization_company_id: Optional[int] = None,
) -> dict[str, Any]:
    """Would-be-auto precision + READY/NOT-READY verdict for one tenant + task.

    Over ``[start, end)``, restricted to rows the shadow evaluator marked
    would-be-auto (``would_autoexecute is True``), reports:

    - ``would_autoexecute_volume`` — how many predictions would have auto-executed
    - ``precision`` — fraction of those the human agreed with (matched_human).
      This is the number that gates promotion.
    - ``high_severity_miss_count`` — would-be-auto items the human overrode; each
      is a prediction we'd have wrongly auto-applied.
    - ``cycles`` — distinct days with would-be-auto activity (a maturity proxy).
    - ``verdict`` — READY only if precision >= bar, zero high-severity misses, and
      enough samples over enough cycles; otherwise NOT_READY with reasons.

    Reads this org's rows only.
    """
    filters = [
        AICorrectionEvent.organization_id == organization_id,
        AICorrectionEvent.task_type == task_type,
        AICorrectionEvent.would_autoexecute.is_(True),
        AICorrectionEvent.created_at >= start,
        AICorrectionEvent.created_at < end,
    ]
    if organization_company_id is not None:
        filters.append(AICorrectionEvent.organization_company_id == organization_company_id)

    rows = db.query(AICorrectionEvent).filter(*filters).all()
    volume = len(rows)
    matched = sum(1 for r in rows if r.matched_human is True)
    misses = sum(1 for r in rows if r.matched_human is not True)
    precision = (matched / volume) if volume else None
    cycles = len({r.created_at.date() for r in rows if r.created_at is not None})

    min_precision = settings.autonomy_promotion_min_precision
    min_samples = settings.autonomy_promotion_min_samples
    min_cycles = settings.autonomy_promotion_min_cycles

    reasons: list[str] = []
    if volume < min_samples:
        reasons.append(f"insufficient_samples ({volume} < {min_samples})")
    if cycles < min_cycles:
        reasons.append(f"insufficient_cycles ({cycles} < {min_cycles})")
    if precision is None or precision < min_precision:
        reasons.append(f"precision_below_bar ({precision} < {min_precision})")
    if misses > 0:
        reasons.append(f"high_severity_misses ({misses})")

    ready = not reasons
    return {
        "organization_id": organization_id,
        "organization_company_id": organization_company_id,
        "task_type": task_type,
        "start": start,
        "end": end,
        "would_autoexecute_volume": volume,
        "precision": precision,
        "high_severity_miss_count": misses,
        "cycles": cycles,
        "bar": {
            "min_precision": min_precision,
            "min_samples": min_samples,
            "min_cycles": min_cycles,
        },
        "verdict": "READY" if ready else "NOT_READY",
        "reasons": reasons,
    }


# ── Free-stage residual / cost-funnel measurement (Phase 3) ──────────────────
# Precision/coverage operating points to report per realm. Coverage = % of lines
# in the confident bucket (confidence >= c AND support >= s); precision = of
# those, % the free model got right vs. the known account. All from persisted
# free-stage events — $0, no LLM.
RESIDUAL_GRID = [(c, s) for c in (0.7, 0.8, 0.9) for s in (3, 5)]
FREE_SOURCE_PREFIX = "glfree:"


def residual_summary(
    db: Session,
    *,
    organization_id: int,
    qbo_realm_id: str,
    start: datetime,
    end: datetime,
    task_type: str = "categorization",
) -> dict[str, Any]:
    """Per-realm precision/coverage curve over the free learned-model results.

    Reads only this realm's free-stage events (source_object_ref ``glfree:*``).
    Returns the full grid (the curve), the point at the configured default split,
    and the residual % (lines the free model is NOT confident on at the default).
    """
    rows = (
        db.query(AICorrectionEvent.ai_confidence, AICorrectionEvent.ai_output, AICorrectionEvent.matched_human)
        .filter(
            AICorrectionEvent.organization_id == organization_id,
            AICorrectionEvent.qbo_realm_id == qbo_realm_id,
            AICorrectionEvent.task_type == task_type,
            AICorrectionEvent.source_object_ref.like(FREE_SOURCE_PREFIX + "%"),
            AICorrectionEvent.created_at >= start,
            AICorrectionEvent.created_at < end,
        )
        .all()
    )
    total = len(rows)

    def _point(conf_min: float, support_min: int) -> dict[str, Any]:
        confident = [
            r for r in rows
            if r.ai_confidence is not None and float(r.ai_confidence) >= conf_min
            and int((r.ai_output or {}).get("support", 0)) >= support_min
        ]
        n = len(confident)
        correct = sum(1 for r in confident if r.matched_human is True)
        return {
            "confidence": conf_min,
            "support": support_min,
            "n_confident": n,
            "n_correct": correct,
            "coverage": (n / total) if total else 0.0,
            "precision": (correct / n) if n else None,
        }

    curve = [_point(c, s) for (c, s) in RESIDUAL_GRID]
    default = _point(settings.autonomy_free_confidence_threshold, settings.autonomy_free_min_support)
    return {
        "organization_id": organization_id,
        "qbo_realm_id": qbo_realm_id,
        "task_type": task_type,
        "start": start,
        "end": end,
        "total_lines": total,
        "default_split": {
            "confidence": settings.autonomy_free_confidence_threshold,
            "support": settings.autonomy_free_min_support,
        },
        "default": default,
        "residual_pct": (1.0 - default["coverage"]) if total else None,
        "curve": curve,
    }

"""
detector.py — AI + rule-based detection engine for AccountingSchedule.

Combines keyword/amount heuristics with a GPT-4o prompt to classify whether
a transaction should become a PREPAID, FIXED_ASSET, INTANGIBLE, or ACCRUAL entry.
Returns a DetectionResult with type, confidence, reasoning, and suggested schedule params.
"""
from __future__ import annotations

import json  # noqa: F401
import logging
import re  # noqa: F401
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Thresholds — per-type keyword patterns now live in the schedule-type
# descriptor registry (app/services/schedule_types.py: detection_keywords,
# detection_priority, detection_confidence, detection_default_life_months).
# PLAN-EXEMPT: ratified plan 20260701-phase-a-generic-schedules (AC-1 branch-site collapse)
# ---------------------------------------------------------------------------
from ..schedule_types import SCHEDULE_TYPES, detection_ordered_descriptors  # noqa: E402

CAPITALIZATION_THRESHOLD = 500.0  # USD — below this, don't capitalise


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------
@dataclass
class DetectionResult:
    schedule_type: Optional[str] = None          # a schedule_types registry code | None
    confidence: float = 0.0                       # 0.0 – 1.0
    reasoning: str = ""
    suggested_name: str = ""
    suggested_useful_life_months: Optional[int] = None
    suggested_asset_class: Optional[str] = None
    suggested_start_date: Optional[date] = None
    flags: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Rule-based detector
# ---------------------------------------------------------------------------
def _rule_based_detect(description: str, vendor_name: str, amount: float,
                       txn_date: date) -> DetectionResult:
    """
    Fast heuristic pass. Returns a DetectionResult; confidence ≤ 0.75 (reserved
    for GPT to confirm or override).
    """
    text = f"{description or ''} {vendor_name or ''}".lower()
    result = DetectionResult(suggested_start_date=txn_date)

    if amount < CAPITALIZATION_THRESHOLD:
        result.reasoning = f"Amount ${amount:.2f} is below capitalisation threshold (${CAPITALIZATION_THRESHOLD})"
        return result  # schedule_type=None

    # Check keywords in registry priority order (detection_priority ascending;
    # preserves the legacy FIXED_ASSET → INTANGIBLE → PREPAID → ACCRUAL
    # precedence and adds the AC-2 preset types after them).
    vendor_print = vendor_name or (description[:40] if description else "Unknown")
    for descriptor in detection_ordered_descriptors():
        for kw in descriptor.detection_keywords:
            if kw in text:
                result.schedule_type = descriptor.code
                result.confidence = descriptor.detection_confidence
                result.reasoning = (
                    f"Description matches {descriptor.detection_reason_label} keyword '{kw}'"
                )
                if descriptor.infer_asset_class:
                    result.suggested_asset_class = _infer_asset_class(kw)
                    result.suggested_useful_life_months = _default_life(kw)
                elif descriptor.detection_default_life_months is not None:
                    result.suggested_useful_life_months = (
                        descriptor.detection_default_life_months
                    )
                result.suggested_name = (
                    f"{descriptor.detection_name_prefix} – {vendor_print}"
                )
                return result

    return result  # no match


def _infer_asset_class(keyword: str) -> str:
    mapping = {
        "computer": "COMPUTER", "laptop": "COMPUTER", "server": "COMPUTER",
        "workstation": "COMPUTER", "monitor": "COMPUTER", "printer": "COMPUTER",
        "vehicle": "VEHICLE", "truck": "VEHICLE", "automobile": "VEHICLE",
        "furniture": "FURNITURE", "fixture": "FURNITURE",
        "machinery": "MACHINERY", "equipment": "MACHINERY", "forklift": "MACHINERY",
        "leasehold": "LEASEHOLD_IMPROVEMENT", "build-out": "LEASEHOLD_IMPROVEMENT",
        "buildout": "LEASEHOLD_IMPROVEMENT", "construction": "LEASEHOLD_IMPROVEMENT",
        "renovation": "LEASEHOLD_IMPROVEMENT",
    }
    for kw, cls in mapping.items():
        if kw in keyword:
            return cls
    return "OTHER"


def _default_life(keyword: str) -> int:
    """MACRS-inspired useful life in months."""
    if any(k in keyword for k in ["computer", "laptop", "server", "workstation", "printer", "monitor"]):
        return 60   # 5 years
    if any(k in keyword for k in ["vehicle", "truck", "automobile"]):
        return 60   # 5 years
    if any(k in keyword for k in ["furniture", "fixture", "machinery", "equipment", "forklift"]):
        return 84   # 7 years
    if any(k in keyword for k in ["leasehold", "build-out", "buildout", "construction", "renovation"]):
        return 180  # 15 years
    return 60


# ---------------------------------------------------------------------------
# AI-enhanced detector
# ---------------------------------------------------------------------------
def detect_with_ai(description: str, vendor_name: str, amount: float,
                   txn_date: date, openai_client=None) -> DetectionResult:
    """
    Run rule-based detection first; if confidence is below 0.80, call the
    configured LLM (Gemini/Claude/OpenAI) for a second opinion.
    The openai_client param is kept for backwards compatibility but is unused.
    """
    rule_result = _rule_based_detect(description, vendor_name, amount, txn_date)

    # Skip AI if rules are highly confident or no type found (below threshold)
    if rule_result.confidence >= 0.80 or rule_result.schedule_type is None:
        return rule_result

    try:
        from ...services.llm import analysis_llm
        prompt = f"""You are an expert accountant. Classify the following business transaction.

Transaction:
  Description: {description or ''}
  Vendor: {vendor_name or 'N/A'}
  Amount: ${amount:,.2f}
  Date: {txn_date}

Initial rule-based classification: {rule_result.schedule_type} (confidence {rule_result.confidence:.0%})

Respond ONLY with a valid JSON object (no markdown) with these keys:
  schedule_type: one of {", ".join(SCHEDULE_TYPES.keys())}, or null
  confidence: float 0.0 to 1.0
  reasoning: one sentence why
  suggested_name: short schedule label (max 60 chars)
  useful_life_months: integer or null
  asset_class: string or null (COMPUTER, VEHICLE, FURNITURE, MACHINERY, LEASEHOLD_IMPROVEMENT, SOFTWARE, TRADEMARK, OTHER)
"""
        data = analysis_llm.predict_json(
            system_prompt="You are an expert accountant. Always respond with valid JSON.",
            user_prompt=prompt,
            temperature=0,
            # PLAN-EXEMPT: ratified plan 20260702-qa-remediation W-1 (F-7):
            # schedule-detection spend is attributed explicitly on the ledger.
            task_type="schedule",
        )
        if data is None:
            return rule_result

        ai_result = DetectionResult(
            schedule_type=data.get("schedule_type"),
            confidence=float(data.get("confidence", 0)),
            reasoning=data.get("reasoning", ""),
            suggested_name=data.get("suggested_name", rule_result.suggested_name),
            suggested_useful_life_months=data.get("useful_life_months"),
            suggested_asset_class=data.get("asset_class"),
            suggested_start_date=txn_date,
        )
        logger.info(f"[Detector] AI result: type={ai_result.schedule_type} conf={ai_result.confidence:.0%}")
        return ai_result

    except Exception as e:
        logger.warning(f"[Detector] AI detection failed, falling back to rule result: {e}")
        return rule_result

"""
app/agent/fpa/interview_state.py
─────────────────────────────────
Session state and data models for the Agent-Driven FP&A / LBO Workbench.
Tracks the interactive dialogue between the AI Modeler and the deal team / CFO.
"""
from __future__ import annotations

import enum
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
import math
from typing import Any, Dict, List, Optional

from ...fpa.uaf.schema import UniversalTrialBalance
from .modes import ForecastMode


class QuestionCategory(str, enum.Enum):
    COA_CLASSIFICATION = "coa_classification"
    EBITDA_ADDBACK = "ebitda_addback"
    DRIVER_ASSUMPTION = "driver_assumption"
    DEBT_STRUCTURE = "debt_structure"
    TRANSACTION_SOURCES_USES = "transaction_sources_uses"
    OPERATING_BUDGET = "operating_budget"
    HEADCOUNT_PLAN = "headcount_plan"
    INTERCOMPANY_ELIMINATION = "intercompany_elimination"


class SessionStatus(str, enum.Enum):
    IN_PROGRESS = "in_progress"
    RESOLVED = "resolved"
    LOCKED = "locked"


@dataclass
class InterviewQuestion:
    question_id: str
    category: QuestionCategory
    title: str
    prompt: str
    options: List[Any]
    recommended_option: str
    confidence: float  # 0.0 to 1.0
    impact_rationale: str
    account_id: Optional[str] = None
    account_name: Optional[str] = None
    dollar_magnitude: float = 0.0
    selected_answer: Optional[str] = None
    user_notes: Optional[str] = None
    is_answered: bool = False
    decision_key: str = ""
    target: Dict[str, Any] = field(default_factory=dict)
    selected_value: Any = None


@dataclass
class AnomalyRecord:
    anomaly_id: str
    anomaly_type: str  # e.g. "abnormal_balance", "ambiguous_classification", "candidate_addback", "debt_tranche"
    account_id: str
    account_name: str
    dollar_amount: float
    description: str
    severity: str = "medium"  # low, medium, high, critical


@dataclass
class InterviewSession:
    session_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    entity_id: str = "default_entity"
    mode: ForecastMode = ForecastMode.OPERATING
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    status: SessionStatus = SessionStatus.IN_PROGRESS
    trial_balance: Optional[UniversalTrialBalance] = None
    anomalies: List[AnomalyRecord] = field(default_factory=list)
    questions: List[InterviewQuestion] = field(default_factory=list)
    resolved_classifications: Dict[str, str] = field(default_factory=dict)
    ebitda_addbacks: List[Dict[str, Any]] = field(default_factory=list)
    debt_tranche_definitions: List[Dict[str, Any]] = field(default_factory=list)
    driver_overrides: Dict[str, Any] = field(default_factory=dict)
    locked_parameters: Optional[Dict[str, Any]] = None
    audit_trail: List[Dict[str, Any]] = field(default_factory=list)
    forecast_result: Optional[Dict[str, Any]] = None
    schema_version: int = 2
    decisions: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    model_inputs: Dict[str, Any] = field(default_factory=dict)
    input_provenance: Dict[str, Any] = field(default_factory=dict)
    source_session_id: Optional[str] = None

    def to_storage_dict(self) -> Dict[str, Any]:
        """Versioned JSON data only; no executable serialization or ORM objects."""
        data = {name: getattr(self, name) for name in self.__dataclass_fields__
                if name not in {"trial_balance", "questions", "anomalies"}}
        data["trial_balance"] = self.trial_balance.to_dict() if self.trial_balance else None
        data["questions"] = [asdict(question) for question in self.questions]
        data["anomalies"] = [asdict(anomaly) for anomaly in self.anomalies]
        return {"schema_version": self.schema_version, "session": _encode_state(data)}

    @classmethod
    def from_storage_dict(cls, data: Dict[str, Any]) -> "InterviewSession":
        if type(data.get("schema_version")) is not int or data["schema_version"] not in (1, 2):
            raise ValueError("Unsupported interview state version")
        values = _decode_state(data["session"])
        if values.get("schema_version", data["schema_version"]) != data["schema_version"]:
            raise ValueError("Interview state version mismatch")
        values["schema_version"] = data["schema_version"]
        values["mode"] = ForecastMode(values["mode"])
        values["status"] = SessionStatus(values["status"])
        tb = values.pop("trial_balance")
        values["trial_balance"] = UniversalTrialBalance.from_dict(tb) if tb else None
        values["questions"] = [InterviewQuestion(**{**q, "category": QuestionCategory(q["category"])})
                               for q in values["questions"]]
        values["anomalies"] = [AnomalyRecord(**a) for a in values["anomalies"]]
        return cls(**values)

    def get_question(self, q_id: str) -> Optional[InterviewQuestion]:
        for q in self.questions:
            if q.question_id == q_id:
                return q
        return None

    def pending_questions_count(self) -> int:
        return sum(1 for q in self.questions if not q.is_answered)

    def log_event(self, action: str, details: Dict[str, Any]) -> None:
        self.audit_trail.append({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "action": action,
            "details": details,
        })


def _encode_state(value: Any) -> Any:
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("Non-finite interview amount")
        return {"$decimal": str(value)}
    if isinstance(value, datetime):
        return {"$datetime": value.isoformat()}
    if isinstance(value, date):
        return {"$date": value.isoformat()}
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("Non-finite interview amount")
    if isinstance(value, dict):
        # Encode mappings as pairs, so user metadata cannot impersonate type tags.
        return {"$mapping": [[str(k), _encode_state(v)] for k, v in value.items()]}
    if isinstance(value, (list, tuple)):
        return [_encode_state(item) for item in value]
    if value is None or type(value) in (str, bool, int, float):
        return value
    raise ValueError("Unsupported interview value")


def _decode_state(value: Any) -> Any:
    if isinstance(value, list):
        return [_decode_state(item) for item in value]
    if isinstance(value, dict):
        if set(value) == {"$mapping"}:
            return {k: _decode_state(v) for k, v in value["$mapping"]}
        if set(value) == {"$decimal"}:
            number = Decimal(value["$decimal"])
            if number.is_finite():
                return number
        if set(value) == {"$datetime"}:
            return datetime.fromisoformat(value["$datetime"])
        if set(value) == {"$date"}:
            return date.fromisoformat(value["$date"])
        raise ValueError("Invalid interview state value")
    return value

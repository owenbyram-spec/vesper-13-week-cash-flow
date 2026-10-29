"""
app/agent/fpa/__init__.py
─────────────────────────
Agent-Driven FP&A / LBO Workbench package.
Provides dialogical interview engine, anomaly detection, and deterministic parameter packing.
"""
from .anomaly_detector import detect_tb_anomalies
from .interview_engine import (
    answer_interview_question,
    auto_resolve_high_confidence,
    generate_interview_questions,
    start_interview_session,
)
from .interview_state import (
    AnomalyRecord,
    InterviewQuestion,
    InterviewSession,
    QuestionCategory,
    SessionStatus,
)
from .parameter_packer import pack_interview_parameters

__all__ = [
    "AnomalyRecord",
    "InterviewQuestion",
    "InterviewSession",
    "QuestionCategory",
    "SessionStatus",
    "answer_interview_question",
    "auto_resolve_high_confidence",
    "detect_tb_anomalies",
    "generate_interview_questions",
    "pack_interview_parameters",
    "start_interview_session",
]

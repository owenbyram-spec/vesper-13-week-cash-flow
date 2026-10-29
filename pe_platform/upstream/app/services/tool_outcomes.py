"""Trusted, in-process tool outcomes; independent of models and transport APIs."""
from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass(frozen=True)
class DraftedToolResult:
    """A producer's exact newly persisted proposal, never an adopted proposal."""

    text: str
    proposal_id: int
    disposition: Literal["drafted", "reused", "auto_executed", "failed", "recovery_required", "blocked"] = "drafted"
    policy: dict[str, Any] = field(default_factory=dict)


class ToolFailure(Exception):
    """An expected failure with safe presentation text and explicit retry semantics."""

    def __init__(self, code: str, text: str, *, retryable: bool = False):
        super().__init__(text)
        self.code = code
        self.text = text
        self.retryable = retryable


@dataclass(frozen=True)
class ToolExecutionOutcome:
    text: str
    status: Literal["succeeded", "failed", "blocked"]
    code: str | None = None
    retryable: bool = False
    action: dict[str, Any] | None = None

    @property
    def succeeded(self) -> bool:
        return self.status == "succeeded"

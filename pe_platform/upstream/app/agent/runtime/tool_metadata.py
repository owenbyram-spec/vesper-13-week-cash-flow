# Plan 20260712-agentic-finance-os W6 (GAP-8, directive §14) — tool-metadata enforcement.
"""Give the previously-DECORATIVE ``source_requirements`` field a runtime consumer.

Until W6, every tool declared ``source_requirements`` (e.g. ["tenant_scoped_data"])
but NOTHING read it — the directive's §14 "decorative metadata" anti-pattern. This
checker is consumed by ToolExecutor: ``check_source_requirements`` gates a tool's execution
against a DataQualityReport (a tool needing ``fresh_sync`` must not run on stale/
failed data), and ``validate_source_requirements`` enforces the field's vocabulary
so a typo'd/unknown requirement fails a test-time gate (metadata is enforced, not
decorative). Per AC-4, additional metadata fields (cost_class/reversibility/retry/
timeout) are DEFERRED until each has its own enforcement point — adding a field
without a consumer would re-introduce exactly the anti-pattern this closes.
"""
from __future__ import annotations

from ...services.data_quality import DataQualityReport
from .types import ToolDefinition

# The closed vocabulary of data preconditions a tool may declare. Discovered by
# turning the field on: the live registry declares tenant_scoped_data (all data
# tools) and audit_trace (the delete/reclassify write tools) — both STRUCTURAL
# (tenant pin / the action authority and audit contract; not a guarantee that
# best-effort AgentStep telemetry persisted). fresh_sync is the
# new data-quality gate a current-period tool may declare.
_STRUCTURAL_REQUIREMENTS = frozenset({"tenant_scoped_data", "audit_trace"})
KNOWN_SOURCE_REQUIREMENTS = _STRUCTURAL_REQUIREMENTS | frozenset({"fresh_sync"})


def check_source_requirements(
    tool: ToolDefinition, report: DataQualityReport | None, *,
    company_id: str | None = None, reporting_period: str | None = None,
) -> tuple[bool, list[str]]:
    """(all-met, unmet-reasons) for a tool's declared source_requirements against the
    current data-quality report. A ``fresh_sync`` requirement is UNMET on a stale/
    failed/missing report — the caller should then refresh/disclose rather than
    present the tool's output as a complete current-period fact."""
    unmet: list[str] = []
    for req in tool.source_requirements or []:
        if req in _STRUCTURAL_REQUIREMENTS:
            continue  # tenant_scoped_data (tenant pin) / audit_trace (executor audit write)
        if req == "fresh_sync":
            if report is not None and company_id is not None and report.company_id != company_id:
                unmet.append("fresh_sync: evidence belongs to another company")
            elif report is not None and reporting_period is not None and reporting_period not in report.covered_periods:
                unmet.append("fresh_sync: requested reporting period is not covered")
            elif report is None or not report.is_fresh:
                status = report.sync_status if report is not None else "unknown"
                unmet.append(f"fresh_sync: data is not fresh (sync_status={status})")
        else:
            # An unknown requirement is fail-closed at runtime — never silently ignored.
            unmet.append(f"unknown source_requirement '{req}' — cannot verify; treating as unmet")
    return (not unmet, unmet)


def validate_source_requirements(tool_defs: list[ToolDefinition]) -> list[str]:
    """Every tool's declared source_requirements must be in the known vocabulary
    (empty == healthy). Enforced by the registry-integrity test so a typo'd/undefined
    requirement — which check_source_requirements would fail-closed on at runtime —
    is caught during registry validation instead."""
    violations: list[str] = []
    for tool in tool_defs:
        for req in tool.source_requirements or []:
            if req not in KNOWN_SOURCE_REQUIREMENTS:
                violations.append(
                    f"tool '{tool.name}' declares unknown source_requirement '{req}' "
                    f"— add it to KNOWN_SOURCE_REQUIREMENTS + a consumer, or remove it"
                )
    return violations

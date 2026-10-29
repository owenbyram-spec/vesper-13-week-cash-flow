from __future__ import annotations

import json  # PLAN-EXEMPT: ratified plan 20260713-wave2-6-tool-hardening chunk C2 (marker RATIFICATION-2026-07-13.md on disk) — dry-run impact serialization
import re
import logging  # PLAN-EXEMPT: P0 proposer FORCE-RLS write instrumentation per Steven 2026-06-23
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager  # PLAN-EXEMPT: ratified plan 20260713-wave2-6-tool-hardening chunk C2 (marker RATIFICATION-2026-07-13.md on disk)
from datetime import date, datetime, timedelta
from time import perf_counter
from typing import Any, Optional

from sqlalchemy import text  # PLAN-EXEMPT: P0 proposer FORCE-RLS write instrumentation per Steven 2026-06-23
from sqlalchemy.orm import Session

from ...models import ProposedAction
# PLAN-EXEMPT: ratified plan 20260713-wave1-learning-flywheel (marker RATIFICATION-2026-07-13.md)
# — C4/AC-10 (SH-2): importing the module registers the ProposedAction before_insert
# fingerprint-stamping listener for EVERY draft lane in this process (chat, router, sweep);
# the post-draft collapse seam below reads the stamped column and its bounded window.
from ...qbo_actions.proposal_fingerprint import PROPOSAL_DEDUP_WINDOW_HOURS
# Plan 20260706-widen-internal-db-approval-execution (C1-b): the internal-DB
# action-type map is derived from the shared service inverse — ONE source of
# truth with the approve endpoint + remediation script (no second hand-copy).
from ...services.internal_action_execution import ACTION_TYPE_TO_TOOL
from ...services.tool_outcomes import DraftedToolResult, ToolExecutionOutcome, ToolFailure
from ...services.llm import build_context_artifact
from ...services.llm_usage import classify_error
from ...services.redaction import redact_to_string
# LG-5 (Wave 3.3): the tool-failure RECORD/EXPIRE lives in a service OUTSIDE this
# app/agent/runtime/* path (the AWB-06 structural no-escape scope) — these are thin
# calls; the Core-form writes are in app/services/tool_failure_memory.py (E-B/E-C).
from ...services.tool_failure_memory import record_tool_failure, resolve_tool_failure
# PLAN-EXEMPT: P0 hotfix (proposer FORCE-RLS write denial) per Steven directive 2026-06-23
from ...tenant_rls import TenantScope, emit_tenant_guc
from ..tracking import AgentRunTracker
# PLAN-EXEMPT: plan 20260711-agentic-workflow-hardening A1 — shared self-drafter constant import
from .tool_registry import HANDLER_LANE_SELF_DRAFTERS
from .tool_policy import ToolPolicyGate, ToolPolicyViolation
from .types import ContextPacket, RiskLevel, ToolDefinition


logger = logging.getLogger(__name__)

SENSITIVE_KEYS = {"access_token", "refresh_token", "password", "secret", "api_key", "oauth", "token"}

# tool_name -> action_type for the internal-DB assistant tools (the exact inverse of the
# service's ACTION_TYPE_TO_TOOL — DERIVED, never hand-copied, so the two maps stay in
# lockstep by construction; C3-TOOLS wave 3.1 grew the set by 8 rows 4-13 writes). Approval
# of these EXECUTES the handler (plan 20260706-widen-internal-db-approval-execution), so their
# drafted payload copy must say so (AC-4); everything else remains review-only.
_INTERNAL_DB_ACTION_TYPE_BY_TOOL = {tool: action for action, tool in ACTION_TYPE_TO_TOOL.items()}

_INTERNAL_DB_APPROVAL_EFFECT = (
    "Approval executes this change in Vesper Bot immediately; the outcome is recorded "
    "honestly as EXECUTED or FAILED on the action."
)
_REVIEW_ONLY_APPROVAL_EFFECT = (
    "Records approval for manual follow-up; does not execute a financial write automatically."
)


# PLAN-EXEMPT: P0 proposer FORCE-RLS write diagnostic per Steven directive 2026-06-23.
def _diag_proposed_actions_write(db: Session, context: ContextPacket, label: str) -> dict:
    """Read the realm GUC ON THE CONNECTION the flush will use, vs the row's company_id, so a prod
    RLS denial reveals whether app.current_realm_id is MISSING (emit didn't reach this txn) or
    MISMATCHED (wrong realm). Returns {realm,org,bypass} (empty on probe failure / non-Postgres).
    Temporary instrumentation — remove once confirmed. No-op-safe."""
    try:
        row = db.execute(
            text(
                "SELECT current_setting('app.current_realm_id', true) AS realm, "
                "current_setting('app.current_org_id', true) AS org, "
                "current_setting('app.rls_bypass', true) AS bypass"
            )
        ).mappings().first()
        out = dict(row) if row else {}
        logger.info(
            "[ProposerDiag/%s] flush-conn GUC realm=%r org=%r bypass=%r | action.company_id=%r ctx.org=%r",
            label, out.get("realm"), out.get("org"), out.get("bypass"), context.company_id, context.organization_id,
        )
        return out
    except Exception:  # pragma: no cover - diagnostic must never break the proposer
        logger.warning("[ProposerDiag/%s] GUC probe failed", label, exc_info=True)
        return {}


# PLAN-EXEMPT: plan 20260711-agentic-workflow-hardening A4 — INV-EXACT comparison baseline:
# the drafted input minus the trusted "_"-prefixed context keys (those are re-injected at
# execution time and are EXPECTED to differ; everything else must survive sanitization intact).
def _strip_private_keys(value: Any) -> Any:  # CRAFT-OK: mirrors sanitize_payload's dynamic tool-input boundary
    if isinstance(value, dict):
        return {str(k): _strip_private_keys(v) for k, v in value.items() if not str(k).startswith("_")}
    if isinstance(value, list):
        return [_strip_private_keys(item) for item in value]
    return value


def sanitize_payload(value: Any, *, limit: int = 8000) -> Any:
    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            key_str = str(key)
            if key_str.startswith("_"):
                continue
            if any(sensitive in key_str.lower() for sensitive in SENSITIVE_KEYS):
                cleaned[key_str] = "[redacted]"
            else:
                cleaned[key_str] = sanitize_payload(item, limit=limit)
        return cleaned
    if isinstance(value, list):
        return [sanitize_payload(item, limit=limit) for item in value[:50]]
    if isinstance(value, (str, int, float, bool)) or value is None:
        if isinstance(value, str) and len(value) > limit:
            return value[:limit] + "...[truncated]"
        return value
    return str(value)


class ToolExecutor:
    def __init__(self, *, db: Session, policy_gate: ToolPolicyGate, tracker: Optional[AgentRunTracker] = None):
        self.db = db
        self.policy_gate = policy_gate
        self.tracker = tracker
        self.last_outcome: ToolExecutionOutcome | None = None

    def execute(self, tool: ToolDefinition, context: ContextPacket, tool_input: Any) -> str:
        """Presentation facade for chat; retain only this call's typed outcome."""
        self.last_outcome = None
        self.last_outcome = self.execute_result(tool, context, tool_input)
        return self.last_outcome.text

    def execute_result(self, tool: ToolDefinition, context: ContextPacket, tool_input: Any) -> ToolExecutionOutcome:
        input_payload = sanitize_payload(tool_input or {})
        step_payload = {
            "tool_input": input_payload,
            "risk_level": tool.risk_level.value,
            "required_role": tool.required_role,
            "current_user_role": context.user_role,
            "mutates_state": tool.mutates_state,
            "external_side_effect": tool.external_side_effect,
            "requires_confirmation": tool.requires_confirmation,
            "creates_proposed_action": tool.creates_proposed_action,
            "tenant_id": context.tenant_id,
            "company_id": context.company_id,
        }
        started = perf_counter()
        step_cm = self.tracker.step(step_type="tool_call", tool_name=tool.name, input_payload=step_payload) if self.tracker else None
        if step_cm:
            step_cm.__enter__()
        try:
            self.policy_gate.require_allowed(tool, tool_input, context)
            prepared_input = self._prepare_input(tool_input, context)
            self._require_sources(tool, context, prepared_input)
            if tool.name == "teach_vesper_bot":
                result = self._propose_memory_update(context, prepared_input)
            elif (
                tool.creates_proposed_action
                and tool.risk_level == RiskLevel.DRAFT_ONLY
                # HANDLER-lane proposal tools: their OWN handler self-drafts the PENDING proposal.
                # PLAN-EXEMPT: plan 20260711-agentic-workflow-hardening A1 — the exclusion set is now
                # the SHARED registry constant HANDLER_LANE_SELF_DRAFTERS (tool_registry.py) instead
                # of an inline literal, validated by registry_integrity so it cannot drift from the
                # classification sets. Each member carries its own safety_expectation
                # AgentEvalFixture in app/agent/evals.py proving proposal-only routing. Behavior for
                # the original three members is byte-identical; reclassify_transaction joins them
                # because its handler self-drafts a RECLASSIFICATION proposal the same way.
                and tool.name not in HANDLER_LANE_SELF_DRAFTERS
            ):
                result = self._propose_tool_action(context, tool, prepared_input)
            elif not tool.handler:
                raise ToolFailure("unknown_tool", f"Unknown tool: {tool.name}")
            else:
                result = tool.handler(self.db, context.company_id, prepared_input)

            if isinstance(result, DraftedToolResult):
                result = self._collapse_duplicate_proposal(context, result)
                if isinstance(result, DraftedToolResult):
                    result = self._maybe_auto_execute_routine_draft(context, result)
            action_metadata = None
            if isinstance(result, DraftedToolResult):
                action_metadata = self._action_metadata(context, result)
                result = result.text

            output_payload = {
                "allowed": True,
                "action": action_metadata,
                "result_summary": sanitize_payload(self._summarize_result(result)),
                "risk_level": tool.risk_level.value,
                "audit_category": tool.audit_category,
                "duration_ms": int((perf_counter() - started) * 1000),
            }
            uncertain = bool(action_metadata and action_metadata["outcome"] in {"recovery_required", "blocked"})
            unsuccessful = uncertain or bool(action_metadata and action_metadata["outcome"] == "failed")
            if step_cm:
                step_cm.finish(status="failed" if unsuccessful else "succeeded", output_payload=output_payload)
            # LG-5 EXPIRE-on-success: idempotently soft-resolve any OPEN failure row
            # for this realm+tool (a no-op UPDATE when none is open). Fail-open — a
            # memory-write failure never breaks the tool call (the service swallows).
            if not unsuccessful:
                resolve_tool_failure(organization_id=context.organization_id,
                    company_id=context.company_id, tool_name=tool.name)
            return ToolExecutionOutcome(text=result, status="blocked" if uncertain else "failed" if unsuccessful else "succeeded",
                code="internal_execution_review_required" if uncertain else None, action=action_metadata)
        except ToolFailure as exc:
            self.db.rollback()
            if step_cm:
                step_cm.finish(status="failed", output_payload={"allowed": True, "error_code": exc.code, "retryable": exc.retryable, "result_summary": exc.text[:1200]}, error=exc.text)
            record_tool_failure(organization_id=context.organization_id, company_id=context.company_id, tool_name=tool.name, error_class="unknown", last_error_redacted=redact_to_string(exc.text, max_length=500), organization_company_id=context.organization_company_id)
            return ToolExecutionOutcome(text=exc.text, status="failed", code=exc.code, retryable=exc.retryable)
        except ToolPolicyViolation as exc:
            self.db.rollback()
            output_payload = {
                "allowed": False,
                "block_reason": exc.reason,
                "risk_level": tool.risk_level.value,
                "required_role": tool.required_role,
                "current_user_role": context.user_role,
                "duration_ms": int((perf_counter() - started) * 1000),
            }
            if step_cm:
                step_cm.finish(status="blocked", output_payload=output_payload, error=exc.reason)
            return ToolExecutionOutcome(text=f"Tool call blocked by Vesper Bot policy: {exc.reason}", status="blocked", code="policy_denied")
        except Exception as exc:
            self.db.rollback()
            safe_error = redact_to_string(str(exc), max_length=500)
            # LG-5 RECORD: this is the genuine tool-RUNTIME failure seam (the
            # ToolPolicyViolation branch above — a deliberate policy block, not a
            # broken tool — is EXCLUDED). Upsert the (org, realm, tool, error_class)
            # aggregate AFTER db.rollback() so the write rides its OWN committed
            # session (the record helper opens SessionLocal). Fail-open (the service
            # swallows) + org-None-skip (no NULL-org orphan). error_class reuses the
            # classify_error closed vocab; last_error_redacted is the already-redacted
            # safe_error (dashboard/audit lane ONLY — NEVER surfaced in the caveat).
            record_tool_failure(
                organization_id=context.organization_id,
                company_id=context.company_id,
                tool_name=tool.name,
                error_class=classify_error(exc),
                last_error_redacted=safe_error,
                organization_company_id=context.organization_company_id,
            )
            if step_cm:
                step_cm.finish(
                    status="failed",
                    output_payload={
                        "allowed": True,
                        "error": safe_error,
                        "duration_ms": int((perf_counter() - started) * 1000),
                    },
                    error=safe_error,
                )
            raise
        finally:
            if step_cm:
                step_cm.__exit__(None, None, None)

    def _require_sources(self, tool: ToolDefinition, context: ContextPacket, prepared_input: dict) -> None:
        from ...services.data_quality import build_data_quality_report
        from .tool_metadata import check_source_requirements

        report = None
        if "fresh_sync" in tool.source_requirements:
            period = context.reporting_period
            if not period or not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", period):
                raise ToolFailure("source_unavailable", "A trusted reporting period is required for this tool.")
            self._validate_source_period(tool, prepared_input, period)
            try:
                report = build_data_quality_report(self.db, context.company_id, required_periods=[period])
            except Exception as exc:
                raise ToolFailure("source_unavailable", "Source completion evidence is temporarily unavailable.", retryable=True) from exc
        met, reasons = check_source_requirements(tool, report, company_id=context.company_id, reporting_period=context.reporting_period)
        if not met:
            raise ToolFailure("source_unavailable", "Source requirements are not met: " + "; ".join(reasons))

    @staticmethod
    def _validate_source_period(tool: ToolDefinition, inputs: dict, expected: str) -> None:
        """Bind fresh-sync coverage to every temporal selector the handler will receive.

        A monthly completion record cannot justify another month, an open range,
        or an unresolved relative period. These checks never accept model-provided
        source evidence or broaden the server's company/reporting-period scope.
        """
        def refuse() -> None:
            raise ToolFailure("source_unavailable", "Tool dates must explicitly match the trusted reporting month; ambiguous or different periods are not covered.")

        def temporal(key: str) -> bool:
            return key in {"period", "month", "year", "date", "start", "end", "as_of", "periods", "months", "years", "dates"} or key.endswith(("_period", "_month", "_year", "_date", "_start", "_end"))

        def month_value(value) -> int:
            text = str(value).strip().lower()
            names = {name: i for i, name in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
            if text in names:
                return names[text]
            if text.isdigit() and 1 <= int(text) <= 12:
                return int(text)
            refuse()

        def explicit_period(value) -> str:
            text = str(value).strip()
            if re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", text):
                return text
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
                refuse()
            try:
                parsed = date.fromisoformat(text)
                return parsed.strftime("%Y-%m")
            except ValueError:
                refuse()

        properties = (tool.input_schema or {}).get("properties") or {}
        declared = {str(key) for key in properties if temporal(str(key))}
        # A declared monthly freshness gate must have an explicit handler scope.
        # Optional schema fields can still default in the handler, so require every
        # declared temporal field, not merely any temporal-looking input anywhere.
        if not declared or any(key not in inputs or inputs[key] is None for key in declared):
            refuse()

        def reject_unknown_selectors(group: dict, *, top_level: bool = False) -> None:
            for key, value in group.items():
                if temporal(str(key)) and (not top_level or str(key) not in declared):
                    refuse()
                if isinstance(value, dict):
                    reject_unknown_selectors(value)
                elif isinstance(value, list):
                    for item in value:
                        if isinstance(item, dict):
                            reject_unknown_selectors(item)

        reject_unknown_selectors(inputs, top_level=True)
        selectors = {key: inputs[key] for key in declared}
        year_keys = [key for key in selectors if key == "year" or key.endswith("_year")]
        month_keys = [key for key in selectors if key == "month" or key.endswith("_month")]
        for key, value in selectors.items():
            if (key == "date" or key.endswith("_date")) and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(value)):
                refuse()
            if key in year_keys:
                if isinstance(value, bool) or str(value) != expected[:4] or not month_keys:
                    refuse()
            elif key in month_keys and not re.fullmatch(r"\d{4}-\d{2}", str(value)):
                if not year_keys or month_value(value) != int(expected[5:]):
                    refuse()
            elif key == "period" and str(value).lower() == "monthly":
                if not month_keys:
                    refuse()
            else:
                values = value if isinstance(value, list) else [value]
                if not values or any(explicit_period(item) != expected for item in values):
                    refuse()
        for start, end in (("start", "end"), ("start_date", "end_date"), ("start_period", "end_period"), ("period_start", "period_end"), ("from_date", "to_date")):
            if (start in selectors) != (end in selectors):
                refuse()
            if start in selectors and str(selectors[start]) > str(selectors[end]):
                refuse()

    def _prepare_input(self, tool_input: Any, context: ContextPacket) -> dict[str, Any]:
        prepared = dict(tool_input or {}) if isinstance(tool_input, dict) else {}
        for key in ("realm_id", "company_id", "tenant_id", "qbo_realm_id", "organization_id", "organization_company_id", "budget_calculation_plan", "_reviewed_budget_plan"):
            prepared.pop(key, None)
        prepared["_current_user_id"] = context.user_id
        prepared["_current_user_email"] = context.user_email
        if context.organization_id is not None:
            prepared["_organization_id"] = context.organization_id
        if context.organization_company_id is not None:
            prepared["_organization_company_id"] = context.organization_company_id
        return prepared

    def _owned_proposal(self, context: ContextPacket, result: DraftedToolResult) -> ProposedAction | None:
        return (self.db.query(ProposedAction).filter(
            ProposedAction.id == result.proposal_id,
            ProposedAction.company_id == context.company_id,
            ProposedAction.status == "PENDING",
            ProposedAction.revision == 1,
            ProposedAction.last_human_editor_user_id.is_(None),
        ).populate_existing().first())

    def _collapse_duplicate_proposal(self, context: ContextPacket, result: DraftedToolResult) -> str | DraftedToolResult:
        """Collapse only this invocation's exact owned, still-pending proposal."""
        drafted = self._owned_proposal(context, result)
        if drafted is None or not drafted.inputs_fingerprint or drafted.plan_step_id is not None:
            return result
        window_start = datetime.utcnow() - timedelta(hours=PROPOSAL_DEDUP_WINDOW_HOURS)
        existing = (
            self.db.query(ProposedAction)
            .filter(
                ProposedAction.company_id == context.company_id,
                ProposedAction.action_type == drafted.action_type,
                ProposedAction.inputs_fingerprint == drafted.inputs_fingerprint,
                ProposedAction.id < drafted.id,
                ProposedAction.status == "PENDING",
                ProposedAction.created_at >= window_start,
            )
            .order_by(ProposedAction.id.desc())
            .first()
        )
        if existing is None:
            return result
        deleted = self.db.query(ProposedAction).filter(
            ProposedAction.id == drafted.id,
            ProposedAction.company_id == context.company_id,
            ProposedAction.status == "PENDING",
            ProposedAction.revision == drafted.revision,
            ProposedAction.inputs_fingerprint == drafted.inputs_fingerprint,
        ).delete(synchronize_session=False)
        if not deleted:
            return result
        self.db.commit()
        return DraftedToolResult(
            f"This change is already drafted and awaiting review — ProposedAction #{existing.id} "
            f"({existing.action_type}) covers the same request, so no duplicate was created. "
            "An editor can approve or reject the existing proposal in the Actions page.",
            proposal_id=existing.id, disposition="reused",
        )

    def _maybe_auto_execute_routine_draft(
        self, context: ContextPacket, result: DraftedToolResult
    ) -> str | DraftedToolResult:
        """Only the surviving exact proposal from this invocation is eligible."""
        from ...services.action_tiers import (
            SideEffectTier,
            maybe_auto_execute_routine_action,
            tier_for_action_type,
        )

        if result.disposition != "drafted":
            return result
        drafted = self._owned_proposal(context, result)
        if drafted is None or drafted.plan_step_id is not None:
            return result
        if tier_for_action_type(drafted.action_type) is not SideEffectTier.INTERNAL_ROUTINE:
            return result
        outcome = maybe_auto_execute_routine_action(
            self.db,
            drafted,
            actor_email=str(context.user_email or "autonomy:internal_routine"),
            actor_role=context.user_role,
        )
        if outcome is None:
            return result
        self.db.refresh(drafted)
        if outcome["executed"] and drafted.status != "EXECUTED":
            return result
        policy = {"level": outcome.get("autonomy_level"), "origin": outcome.get("policy_origin"), "actor": outcome.get("authority_actor")}
        if outcome["executed"]:
            return DraftedToolResult(
                f"Applied automatically: ProposedAction #{drafted.id} ({drafted.action_type}) is a "
                "routine internal update, so it was executed under this workspace's routine-write "
                "autonomy policy and recorded in the audit log. "
                f"Result: {str(outcome['message'])[:500]} "
                "(An admin can turn routine-write autonomy off in autonomy settings.)",
                proposal_id=drafted.id, disposition="auto_executed", policy=policy,
            )
        recovery = outcome.get("execution_status") in {"recovery_required", "running", "legacy", "blocked"}
        return DraftedToolResult(
            f"ProposedAction #{drafted.id} ({drafted.action_type}): {str(outcome['message'])[:500]} "
            "Review its execution evidence on the Actions page.",
            proposal_id=drafted.id,
            disposition=("blocked" if outcome.get("execution_status") == "blocked" else "recovery_required") if recovery else "failed",
            policy=policy,
        )

    def _action_metadata(self, context: ContextPacket, result: DraftedToolResult) -> dict | None:
        from ...services.action_tiers import tier_for_action_type
        action = self.db.query(ProposedAction).filter(
            ProposedAction.id == result.proposal_id, ProposedAction.company_id == context.company_id,
        ).populate_existing().first()
        if action is None:
            return None
        from ...services.internal_action_execution import internal_execution_summary
        summary = internal_execution_summary(self.db, action)
        disposition = "failed" if action.status == "FAILED" else result.disposition
        if summary and summary["execution_status"] in {"running", "recovery_required", "legacy", "blocked"}:
            disposition = "blocked" if summary["execution_status"] == "blocked" else "recovery_required"
        if disposition == "auto_executed" and action.status != "EXECUTED":
            disposition = "failed" if action.status == "FAILED" else "drafted"
        tier = tier_for_action_type(action.action_type).value
        policy = result.policy or None
        if policy is None and tier == "internal_routine" and context.organization_id is not None:
            from ...services.task_autonomy import effective_policy, INTERNAL_ROUTINE_TASK_TYPE
            policy = effective_policy(self.db, context.organization_id, INTERNAL_ROUTINE_TASK_TYPE)
        return {"id": action.id, "revision": action.revision, "tier": tier,
                "status": action.status, "outcome": disposition, "authority_actor": action.reviewed_by,
                "policy": policy, "internal_execution": summary}

    def _summarize_result(self, result: Any) -> str:
        text = str(result or "")
        if len(text) <= 1200:
            return text
        try:
            artifact = build_context_artifact(text, purpose="Summarize agent tool output for audit trace.")
            summary = artifact.get("compressed_text") or artifact.get("summary")
            if summary:
                return str(summary)[:1200]
        except Exception:
            pass
        return text[:1200] + "...[truncated]"

    def _propose_memory_update(self, context: ContextPacket, tool_input: dict[str, Any]) -> str | DraftedToolResult:
        fact = str(tool_input.get("fact") or "").strip()
        category = str(tool_input.get("category") or "General").strip()
        if not fact:
            return "Error: You must provide a fact to learn."
        # PLAN-EXEMPT: ratified plan 20260713-wave2-6-tool-hardening chunk C2 (marker
        # RATIFICATION-2026-07-13.md on disk) — decision 4 / AC-3: the teach dry branch.
        # Validation above ran (an empty fact reports the same error a real call would);
        # a dry run drafts NOTHING (zero MEMORY_UPDATE rows) and names no proposal id.
        # teach_vesper_bot's proposal-only routing stays behaviorally proven by its
        # safety_expectation AgentEvalFixture teach_vesper_bot_proposal (app/agent/evals.py);
        # this branch adds no write capability — it removes the write for previews.
        if bool(tool_input.get("_dry_run")):
            return (
                "DRY RUN — nothing was written and no memory proposal was drafted. A real "
                "teach_vesper_bot call would draft a MEMORY_UPDATE ProposedAction to remember "
                f"under '{category}': {fact[:500]} — an editor must approve it before "
                "Vesper Bot stores permanent knowledge."
            )
        action = ProposedAction(
            company_id=context.company_id,
            action_type="MEMORY_UPDATE",
            payload={
                "category": category,
                "content": fact,
                "source": "ai_assistant_chat",
                "source_metadata": {
                    "conversation_id": context.conversation_id,
                    "agent_run_id": context.run_id,
                    "user_id": context.user_id,
                },
            },
            status="PENDING",
            source="ai_assistant",
            category="memory_update",
            priority="medium",
        )
        # PLAN-EXEMPT: emit realm GUC before the FORCE-RLS proposed_actions INSERT (2026-06-23)
        self._pin_write_scope(context)
        self.db.add(action)
        self.db.commit()
        self.db.refresh(action)
        return DraftedToolResult((
            "I drafted a memory update for approval instead of writing permanent knowledge directly. "
            f"ProposedAction #{action.id}: remember under '{category}'."
        ), proposal_id=action.id)

    # PLAN-EXEMPT: P0 hotfix (proposer FORCE-RLS write denial) per Steven directive 2026-06-23.
    def _pin_write_scope(self, context: ContextPacket) -> None:
        """Emit the tenant GUCs on the proposer's OWN write transaction before a
        ProposedAction INSERT.

        proposed_actions is realm-keyed under FORCE RLS (the policy WITH CHECK is
        company_id = app.current_realm_id). In the live streaming agent run the realm-keyed
        INSERT was RLS-denied ("new row violates row-level security policy") while the
        org-keyed agent_runs/agent_steps writes in the SAME run succeeded — relying solely
        on the run-loop's _stream_run_scope listener proved insufficient for this write.
        Emitting the org+realm GUC directly on self.db.connection() guarantees the WITH
        CHECK passes on the exact INSERT transaction, immune to listener/threadpool-pump
        timing (the proposer-side analogue of how close_orchestration self-pins realm scope
        before it proposes). No-op on SQLite.
        """
        emit_tenant_guc(
            self.db.connection(),
            TenantScope(org_id=context.organization_id, realm_id=context.company_id),
        )

    # PLAN-EXEMPT: ratified plan 20260713-wave2-6-tool-hardening chunk C2 (marker
    # RATIFICATION-2026-07-13.md on disk) — AC-2: the savepoint preview machinery,
    # GENERALIZED (not duplicated) out of the two preview helpers below so the
    # dry_run lane and both impact-diff lanes share ONE zero-write mechanism.
    @contextmanager
    def _preview_savepoint(self) -> Iterator[None]:
        """SAVEPOINT + commit→flush rebind: run a committing service with ZERO rows persisted.

        (1) open a SAVEPOINT (``begin_nested``, dialect-portable across Postgres +
        the sqlite test lane), (2) temporarily rebind the session's ``commit`` to
        ``flush`` for the duration of the block so an inner service commit does NOT
        escape the savepoint (a bare commit on a nested transaction would otherwise
        commit the whole transaction), then (3) roll the SAVEPOINT back — discarding
        every flushed change. The block's computed result is the truth of what a
        real call WOULD write; nothing is written.
        """
        savepoint = self.db.begin_nested()
        real_commit = self.db.commit
        # Neutralize the service's inner commit → flush, so its writes stay INSIDE the savepoint
        # and are undone by the rollback below (the proposal gate: zero rows written from chat).
        self.db.commit = self.db.flush  # type: ignore[method-assign]  # PREVIEW-OK: scoped rebind, restored in finally
        try:
            yield
        finally:
            self.db.commit = real_commit  # type: ignore[method-assign]  # restore the real commit
            # Roll the savepoint back so the preview writes ZERO rows (the proposal gate).
            if savepoint.is_active:
                savepoint.rollback()

    def _preview_apply_budget_drivers(self, context: ContextPacket, tool_input: dict) -> dict:
        """Plan without writes; a missing reviewable calculation refuses drafting."""
        from ...services.budget_reforecast import BudgetPlanError, plan_budget_drivers
        try:
            return plan_budget_drivers(
                self.db, company_id=context.company_id,
                budget_year=int(tool_input.get("budget_year") or datetime.utcnow().year),
                scenario=str(tool_input.get("scenario") or "base").strip() or "base",
                force=bool(tool_input.get("force")),
            )
        except (BudgetPlanError, ValueError, TypeError) as exc:
            raise ToolFailure("budget_plan_invalid", str(exc)) from exc

    def _preview_high_risk_service(self, context: ContextPacket, tool_name: str, tool_input: dict) -> dict | None:
        """Compute the row-14/15/16 (HIGH-risk) before/after impact WITHOUT persisting it (AC-3).

        The covenant-config + ASC-842 lease services all end with a ``db.commit()`` and return a
        ``{before, after, ...}`` dict. To surface that impact to the approver while writing ZERO
        rows, this runs under the shared ``_preview_savepoint`` mechanism (savepoint +
        commit→flush rebind + rollback — the row-13 pattern, generalized). The entity is
        resolved by id UNDER the realm scope (a cross-tenant id is not found → no preview,
        honest). Failure is non-fatal: the proposal still drafts, just without an enriched
        diff (a diff we cannot compute is omitted, NEVER fabricated). Returns
        ``{before, after}`` or None."""
        from ...models import DebtCovenant, LeaseSchedule

        realm_id = context.company_id
        # PLAN-EXEMPT: ratified plan 20260713-wave2-6-tool-hardening chunk C2 (marker
        # RATIFICATION-2026-07-13.md on disk) — AC-2: rebased onto the shared
        # _preview_savepoint helper (generalize, don't duplicate). ExitStack keeps the
        # branch body below byte-identical (no reindent) while the shared mechanism
        # guarantees restore + rollback in the finally.
        preview_guard = ExitStack()
        preview_guard.enter_context(self._preview_savepoint())
        try:
            if tool_name == "update_covenant":
                from ...services.covenant_config import update_covenant_service
                covenant = (
                    self.db.query(DebtCovenant)
                    .filter(DebtCovenant.id == tool_input.get("covenant_id"), DebtCovenant.company_id == realm_id)
                    .first()
                )
                updates = tool_input.get("updates")
                if covenant is None or not isinstance(updates, dict) or not updates:
                    return None
                result = update_covenant_service(self.db, realm_id, covenant, updates)
                return {"before": result.get("before"), "after": result.get("after")}
            if tool_name == "update_ebitda_config":
                from ...services.covenant_config import update_ebitda_config_service
                addbacks = tool_input.get("addbacks")
                if not isinstance(addbacks, list):
                    return None
                result = update_ebitda_config_service(self.db, realm_id, addbacks)
                return {"before": result.get("before"), "after": result.get("after")}
            if tool_name == "propose_lbo_assumptions":
                # wave 3.3a: preview the assumption-merge write via the shared core (it ends with a
                # db.commit(), neutralized to a flush above so the merged driver_data stays inside
                # the savepoint and is rolled back — zero rows written from chat). A cross-tenant /
                # missing scenario resolves to None (honest, no fabricated diff).
                from ...lbo.assumption_write import LboAssumptionWriteError, apply_lbo_assumptions
                scenario_id = tool_input.get("scenario_id")
                updates = tool_input.get("updates")
                if not scenario_id or not isinstance(updates, dict) or not updates:
                    return None
                try:
                    result = apply_lbo_assumptions(self.db, realm_id, int(scenario_id), updates, merge=True)
                except LboAssumptionWriteError:
                    return None
                return {"before": result.get("before"), "after": result.get("after")}
            if tool_name in {"activate_lease", "remeasure_lease", "terminate_lease"}:
                lease = (
                    self.db.query(LeaseSchedule)
                    .filter(LeaseSchedule.id == tool_input.get("lease_id"), LeaseSchedule.realm_id == realm_id)
                    .first()
                )
                if lease is None:
                    return None
                submitted_by = str(context.user_email or "AI Assistant")
                if tool_name == "activate_lease":
                    from ...services.lease_service import activate_lease_service
                    result = activate_lease_service(
                        self.db, realm_id, lease,
                        idc_source_account_id=tool_input.get("idc_source_account_id"),
                        idc_source_account_name=tool_input.get("idc_source_account_name"),
                        prepaid_source_account_id=tool_input.get("prepaid_source_account_id"),
                        prepaid_source_account_name=tool_input.get("prepaid_source_account_name"),
                        incentive_source_account_id=tool_input.get("incentive_source_account_id"),
                        incentive_source_account_name=tool_input.get("incentive_source_account_name"),
                        submitted_by=submitted_by,
                    )
                elif tool_name == "remeasure_lease":
                    from ...services.lease_lifecycle import remeasure_lease_service
                    effective_period = str(tool_input.get("effective_period") or "").strip()
                    if not effective_period:
                        return None
                    result = remeasure_lease_service(
                        self.db, realm_id, lease,
                        effective_period=effective_period,
                        new_payment_amount=tool_input.get("new_payment_amount"),
                        new_escalation_steps=tool_input.get("new_escalation_steps"),
                        new_term_months=tool_input.get("new_term_months"),
                        new_discount_rate_annual=tool_input.get("new_discount_rate_annual"),
                        new_rate_basis=tool_input.get("new_rate_basis"),
                        rate_unchanged_reason=tool_input.get("rate_unchanged_reason"),
                        submitted_by=submitted_by,
                    )
                else:  # terminate_lease
                    from datetime import date as _date
                    from ...services.lease_lifecycle import terminate_lease_service
                    raw = str(tool_input.get("termination_date") or "").strip()
                    if not raw:
                        return None
                    result = terminate_lease_service(
                        self.db, realm_id, lease,
                        termination_date=_date.fromisoformat(raw),
                        settlement_amount=tool_input.get("settlement_amount") or 0,
                        settlement_account_id=tool_input.get("settlement_account_id"),
                        settlement_account_name=tool_input.get("settlement_account_name"),
                        gain_loss_account_id=tool_input.get("gain_loss_account_id"),
                        gain_loss_account_name=tool_input.get("gain_loss_account_name"),
                        skip_draft_rows=bool(tool_input.get("skip_draft_rows")),
                        submitted_by=submitted_by,
                    )
                return {"before": result.get("before"), "after": result.get("after")}
            return None
        except Exception:  # pragma: no cover - preview is best-effort; the proposal still drafts
            logger.warning("high-risk service preview failed for %s; drafting without a diff", tool_name, exc_info=True)
            return None
        finally:
            # PLAN-EXEMPT: ratified plan 20260713-wave2-6-tool-hardening chunk C2 — the
            # shared _preview_savepoint finally (restore commit + roll the savepoint back)
            # runs on close; zero rows are written (the proposal gate).
            preview_guard.close()

    # PLAN-EXEMPT: ratified plan 20260713-wave2-6-tool-hardening chunk C2 (marker
    # RATIFICATION-2026-07-13.md on disk) — decision 4 / AC-2: the truthful dry-run report.
    def _dry_run_report(
        self,
        tool: ToolDefinition,
        action_type: str,
        affected_entity: object,
        before_state: object,
        after_state: object,
        is_internal_db: bool,
    ) -> str:
        """The DRY-RUN result text: describe the ProposedAction a real call WOULD draft.

        Truthfulness contract: NEVER carries a proposal id (nothing was drafted, so
        there is no id to name — a fabricated or foreshadowed id would read as a live
        proposal); the states are the SAME savepoint-computed previews / stored-payload
        description the real draft would carry, sanitized through the same boundary.
        """
        approval_effect = _INTERNAL_DB_APPROVAL_EFFECT if is_internal_db else _REVIEW_ONLY_APPROVAL_EFFECT
        impact = json.dumps(
            sanitize_payload({"before_state": before_state, "after_state": after_state}),
            default=str,
        )
        if len(impact) > 4000:
            impact = impact[:4000] + "...[truncated]"
        return (
            f"DRY RUN — nothing was written and no proposal was drafted. A real {tool.name} call "
            f"would draft a ProposedAction ({action_type}) affecting {affected_entity} for human "
            f"review; nothing executes from chat. On approval: {approval_effect} "
            f"Would-be impact: {impact}"
        )

    def _propose_tool_action(self, context: ContextPacket, tool: ToolDefinition, tool_input: dict[str, Any]) -> str | DraftedToolResult:
        sanitized_input = sanitize_payload(tool_input)
        # PLAN-EXEMPT: ratified plan 20260713-wave2-6-tool-hardening chunk C2 — decision 4:
        # the model-sent preview flag. Underscore-prefixed, so BOTH sanitize_payload (the
        # stored payload["tool_input"]) and _strip_private_keys (the INV-EXACT baseline)
        # drop it structurally: present-false is byte-identical to absent for the stored
        # payload AND its before_insert fingerprint (pinned by tests/test_dry_run.py).
        dry_run = bool(tool_input.get("_dry_run"))
        is_internal_db = tool.name in _INTERNAL_DB_ACTION_TYPE_BY_TOOL
        # PLAN-EXEMPT: plan 20260711-agentic-workflow-hardening A4 — INV-EXACT (SYNTHESIS GAP-A5).
        # Internal-DB drafts EXECUTE the stored payload["tool_input"] at approval time, so the
        # stored copy must be semantically identical to the drafted input. sanitize_payload
        # exists for display/telemetry safety (drops _-prefixed context keys, redacts
        # sensitive-named keys, truncates >50-item lists / >8000-char strings) — before this
        # guard, a 60-row update silently executed as 50 rows and a legitimately-named
        # "*_token" field executed as the literal string "[redacted]". A draft whose
        # executable copy sanitization would ALTER is refused loudly instead of drafted
        # silently wrong. Display-only (review_only) drafts are unaffected.
        if is_internal_db and sanitized_input != _strip_private_keys(tool_input):
            return (
                f"I could not draft this {tool.name} change: the request payload contains "
                "fields the proposal store cannot carry faithfully (an over-size list or "
                "text field, or a sensitive-named key), and drafting it would silently "
                "alter what executes on approval. Nothing was drafted. Please split the "
                "request into smaller changes or rename/remove the offending field."
            )
        from ...assistant.handlers import PROPOSAL_REQUEST_VALIDATORS

        request_validator = PROPOSAL_REQUEST_VALIDATORS.get(tool.name)
        if request_validator is not None:
            # Refuse a request the approval-time handler would refuse, before drafting it.
            request_validator(self.db, context.company_id, tool_input)
        action_type = _INTERNAL_DB_ACTION_TYPE_BY_TOOL.get(tool.name, f"ASSISTANT_{tool.name.upper()}")
        affected_entity = (
            tool_input.get("affected_entity")
            or tool_input.get("account_name")
            or tool_input.get("title")
            or tool_input.get("name")
            or tool_input.get("schedule_id")
            or tool_input.get("task_id")
            or tool_input.get("entity_name")
            or tool.name
        )
        # AC-3 (row 13, HIGH bulk overwrite): compute the before/after impact diff WITHOUT
        # persisting it, so the approver sees the full impact in the payload. None for every
        # other tool (or if the preview failed) — then the generic before/after copy is kept.
        impact_preview = (
            self._preview_apply_budget_drivers(context, tool_input)
            if tool.name == "apply_budget_drivers"
            else None
        )
        if impact_preview is not None and not impact_preview.get("driver_updates"):
            return "No budget drivers found to apply; nothing was changed or drafted."
        # AC-3 (rows 14/15/16, HIGH-risk breach-detection / ASC-842 statement impact): compute the
        # before/after snapshot WITHOUT persisting it (a savepoint rolled back), so the approver sees
        # the exact delta. None if the preview couldn't be computed — then the generic copy is kept
        # (a diff we cannot compute is omitted, NEVER fabricated).
        high_risk_preview = (
            self._preview_high_risk_service(context, tool.name, tool_input)
            if tool.name in {
                "update_covenant", "update_ebitda_config",
                "activate_lease", "remeasure_lease", "terminate_lease",
                "propose_lbo_assumptions",  # wave 3.3a — before/after driver_data diff
            }
            else None
        )
        if impact_preview and impact_preview.get("before") is not None:
            before_state = {"budget_lines": impact_preview["before"]}
        elif high_risk_preview and high_risk_preview.get("before") is not None:
            before_state = {"snapshot": sanitize_payload(high_risk_preview["before"])}
        else:
            before_state = {
                "status": "not_loaded",
                "note": "Reviewer should inspect current records before approval.",
            }
        if impact_preview and impact_preview.get("after") is not None:
            after_state = {
                "budget_lines": impact_preview["after"],
                "applied": impact_preview.get("applied"),
                "parents_updated": impact_preview.get("parents_updated"),
            }
        elif high_risk_preview and high_risk_preview.get("after") is not None:
            after_state = {"snapshot": sanitize_payload(high_risk_preview["after"])}
        else:
            after_state = sanitized_input
        # PLAN-EXEMPT: ratified plan 20260713-wave2-6-tool-hardening chunk C2 (marker
        # RATIFICATION-2026-07-13.md on disk) — decision 4 / AC-2: the dry branch. The
        # preview machinery above already ran (savepoint-backed for the preview-capable
        # tools — zero rows by construction; the stored-payload description otherwise),
        # so the report is the same truth the real draft would carry. Return BEFORE the
        # scope pin + INSERT below: a dry run writes ZERO ProposedAction rows and its
        # text never carries a proposal id.
        if dry_run:
            return self._dry_run_report(tool, action_type, affected_entity, before_state, after_state, is_internal_db)
        # Every proposal-gated tool routing through here carries a safety_expectation
        # AgentEvalFixture in app/agent/evals.py (proposal_only_for_financial_mutation) — the
        # behavioral proof this drafts a ProposedAction rather than a direct write.
        action = ProposedAction(
            company_id=context.company_id,
            action_type=action_type,
            payload={
                **({"budget_calculation_plan": impact_preview} if impact_preview is not None else {}),
                "tool_name": tool.name,
                "summary": f"Review requested assistant action: {action_type}",
                # AC-4 truthful copy: internal-DB drafts execute on approval; only
                # non-internal assistant drafts remain review-only.
                "execution_mode": "internal_db" if is_internal_db else "review_only",
                "approval_effect": _INTERNAL_DB_APPROVAL_EFFECT if is_internal_db else _REVIEW_ONLY_APPROVAL_EFFECT,
                "tool_input": sanitized_input,
                "affected_entity": affected_entity,
                "before_state": before_state,
                "after_state": after_state,
                "source_data": {
                    "source": "ai_assistant_chat",
                    "conversation_id": context.conversation_id,
                    "agent_run_id": context.run_id,
                    "user_id": context.user_id,
                    "tool_name": tool.name,
                },
                "required_role": "EDITOR",
            },
            reasoning=f"Drafted by Vesper Bot assistant for review: {tool.description[:500]}",
            confidence_score=0.8,
            status="PENDING",
            source="ai_assistant",
            category=tool.audit_category,
            priority="medium",
            expires_at=datetime.utcnow() + timedelta(days=7),
        )
        # PLAN-EXEMPT: emit realm GUC + diagnostic before the FORCE-RLS proposed_actions INSERT (2026-06-23)
        self._pin_write_scope(context)
        guc = _diag_proposed_actions_write(self.db, context, "tool")
        self.db.add(action)
        try:
            self.db.commit()
        except Exception as exc:
            # Surface the flush-connection GUC in the error so the run-record step error reveals
            # whether app.current_realm_id was MISSING or MISMATCHED at the denied INSERT.
            _diag_msg = (
                f"[ProposerDiag] proposed_actions INSERT denied: flush_conn_realm_guc={guc.get('realm')!r} "
                f"org_guc={guc.get('org')!r} bypass={guc.get('bypass')!r} action.company_id={context.company_id!r} "
                f"ctx.org={context.organization_id!r}; cause={redact_to_string(exc)}"
            )
            raise RuntimeError(_diag_msg) from exc
        self.db.refresh(action)
        return DraftedToolResult((
            f"I drafted this change for approval instead of writing it directly. "
            f"ProposedAction #{action.id} ({action_type}) affects {affected_entity}. "
            "An editor must review and approve it before Vesper Bot changes financial records."
        ), proposal_id=action.id)

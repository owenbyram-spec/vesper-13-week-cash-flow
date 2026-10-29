"""Standalone, non-mutating evaluation with explicit fixtures or provider responses.

Provider requests never contain the hidden scorer or another model's response.
Tools are proposed only; no application tool executor or model loop is invoked.
Lazy package exports keep these imports separate from application execution modules.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from ..performance import MODEL_PRICING_PER_MILLION, estimate_model_cost_usd
from ..runtime.financial_calculator import FinancialCalculator
from ..runtime.model_adapters import AnthropicMessagesAdapter, GoogleGeminiAdapter, OpenAIChatAdapter
from ..runtime.types import ContextPacket, OutputMode, RiskLevel, ToolDefinition
from .oracle import ScenarioOracle

MAX_RUNS = 100
MAX_OUTPUT_TOKENS = 8192
MAX_RESPONSE_BYTES = 65536
PRICING_SOURCE = "repository:app/agent/performance.py:" + hashlib.sha256(
    json.dumps(MODEL_PRICING_PER_MILLION, sort_keys=True, default=str).encode()
).hexdigest()[:16]
ADAPTERS = {"anthropic": AnthropicMessagesAdapter, "openai": OpenAIChatAdapter, "google": GoogleGeminiAdapter}


def strict_json_loads(source: str):
    """Reject ambiguous members at every nesting level before scoring."""
    def object_members(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON member")
            result[key] = value
        return result

    def reject_constant(value):
        raise ValueError("Nonfinite JSON number")

    return json.loads(source, object_pairs_hook=object_members, parse_constant=reject_constant)


def _google_tools(tools: list[dict]) -> list[dict]:
    """Project static schemas into the pinned SDK's supported schema subset.

    Unsupported scalar constraints remain explicit model instructions. This is a
    proposal-only evaluation, not argument validation or permission to execute.
    Unsupported structural alternatives fail before SDK invocation.
    """
    supported = {"type", "format", "description", "nullable", "enum", "items", "properties", "required"}
    scalar_constraints = {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
                          "minLength", "maxLength", "pattern", "minItems", "maxItems", "uniqueItems",
                          "additionalProperties", "default", "title"}
    nodes = 0

    def project(schema, depth=0):
        nonlocal nodes
        nodes += 1
        if type(schema) is not dict or depth > 16 or nodes > 4096:
            raise ValueError("Unsupported or excessive Google tool schema")
        extra = set(schema) - supported
        if extra - scalar_constraints or any(isinstance(schema[k], (dict, list)) for k in extra):
            raise ValueError("Unsupported Google tool schema constraint")
        result = {k: v for k, v in schema.items() if k in supported - {"items", "properties"}}
        if "items" in schema:
            result["items"] = project(schema["items"], depth + 1)
        if "properties" in schema:
            if type(schema["properties"]) is not dict:
                raise ValueError("Unsupported Google tool properties")
            result["properties"] = {name: project(value, depth + 1) for name, value in schema["properties"].items()}
        if extra:
            constraints = json.dumps({k: schema[k] for k in sorted(extra)}, sort_keys=True, allow_nan=False)
            result["description"] = (str(result.get("description", "")) +
                " Schema constraints (proposal instructions, not SDK-enforced): " + constraints).strip()
        return result

    return [{"function_declarations": [{**declaration, "parameters": project(declaration["parameters"])}
             for declaration in tool["function_declarations"]]} for tool in tools]


@dataclass(frozen=True)
class EvaluationTask:
    id: str
    question: str
    context: dict[str, Any]
    output_schema: dict[str, Any]
    version: str = "2026-09-26.2"
    tools: tuple[dict[str, Any], ...] = ()
    oracle: ScenarioOracle | None = field(default=None, repr=False)
    lexical_fixture: Any = field(default=None, repr=False)

    def public_input(self) -> dict[str, Any]:
        # Deliberate projection; asdict(self) would leak private scoring targets.
        return {"task": self.question, "context": self.context, "output_schema": self.output_schema}


def scenario_task(scenario) -> EvaluationTask:
    return EvaluationTask(scenario.id, scenario.task_prompt, scenario.context_data,
        {"entries": [{"account_number": "exact account ID", "entity_id": "entity ID where specified", "debit": "decimal amount", "credit": "decimal amount"}],
         "metrics": scenario.metric_schema}, version=scenario.version, oracle=scenario.oracle)


def _tool(schema: dict[str, Any]) -> ToolDefinition:
    return ToolDefinition(name=schema["name"], namespace="evaluation", description=schema.get("description", ""),
        input_schema=schema.get("input_schema", {}), handler=None, risk_level=RiskLevel.READ_ONLY,
        required_role="VIEWER", mutates_state=False, external_side_effect=False, requires_confirmation=False,
        creates_proposed_action=False, allowed_intents={"general"}, source_requirements=[], audit_category="evaluation",
        pii_sensitivity="synthetic", log_redaction_policy="none", idempotency_key_requirement="none")


def _context() -> ContextPacket:
    return ContextPacket(run_id=None, conversation_id=None, user_id=None, user_email=None, tenant_id=None,
        company_id="synthetic-evaluation", qbo_realm_id="synthetic-evaluation", user_role="VIEWER",
        request_text="Standalone evaluation", intent="general", risk_level=RiskLevel.READ_ONLY, output_mode=OutputMode.MARKDOWN)


class SDKClient:
    """Explicit supported SDK dispatch, with SDK timeout and no automatic retries."""
    def invoke(self, *, provider: str, model: str, request: dict, max_output_tokens: int, timeout_seconds: float):
        payload = {k: v for k, v in request.items() if k != "metadata" and v != []}
        if provider == "openai":
            from openai import OpenAI
            with OpenAI(base_url="https://api.openai.com/v1", timeout=timeout_seconds, max_retries=0) as client:
                return client.chat.completions.create(model=model, max_completion_tokens=max_output_tokens, **payload)
        if provider == "anthropic":
            from anthropic import Anthropic
            with Anthropic(base_url="https://api.anthropic.com", timeout=timeout_seconds, max_retries=0) as client:
                return client.messages.create(model=model, max_tokens=max_output_tokens, **payload)
        if provider == "google":
            import os
            import google.generativeai as genai
            key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
            if not key:
                raise RuntimeError("Google evaluation credentials are unavailable")
            genai.configure(api_key=key)
            client = genai.GenerativeModel(model_name=model, system_instruction=payload.get("system_instruction", ""),
                tools=_google_tools(payload.get("tools", [])) or None,
                generation_config={"max_output_tokens": max_output_tokens})
            return client.generate_content(payload["contents"], request_options={"timeout": timeout_seconds, "retry": None})
        raise ValueError("Unsupported evaluation provider")


def _usage_present(response, provider: str) -> bool:
    usage = getattr(response, "usage_metadata" if provider == "google" else "usage", None)
    if usage is None:
        return False
    fields = ("prompt_token_count", "candidates_token_count") if provider == "google" else ("input_tokens", "output_tokens") if provider == "anthropic" else ("prompt_tokens", "completion_tokens")
    return all(type(getattr(usage, name, None)) is int and getattr(usage, name) >= 0 for name in fields)


class EvaluationRunner:
    def __init__(self, *, client=None, clock: Callable[[], float] = time.perf_counter, max_runs: int = 20,
                 max_output_tokens: int = 2048, timeout_seconds: float = 30):
        if type(max_runs) is not int or not 1 <= max_runs <= MAX_RUNS:
            raise ValueError("max_runs must be between 1 and 100")
        if type(max_output_tokens) is not int or not 1 <= max_output_tokens <= MAX_OUTPUT_TOKENS:
            raise ValueError("max_output_tokens must be between 1 and 8192")
        if not 0 < timeout_seconds <= 120:
            raise ValueError("timeout_seconds must be positive and at most 120")
        self.client = client
        self.clock = clock
        self.max_runs = max_runs
        self.max_output_tokens = max_output_tokens
        self.timeout_seconds = timeout_seconds
        self.calls = 0

    def run(self, task: EvaluationTask, *, mode: str, provider: str | None = None, model: str | None = None,
            fixture: dict | None = None, calculator: bool = False) -> dict:
        if mode not in {"fixture", "provider"}:
            raise ValueError("mode must be fixture or provider")
        if self.calls >= self.max_runs:
            raise ValueError("Evaluation run count exceeded")
        if mode == "provider" and (provider not in ADAPTERS or not model or fixture is not None):
            raise ValueError("Provider mode requires a supported provider/model and forbids fixture fallback")
        self.calls += 1
        public = task.public_input()
        request_material = {**public, "tools": list(task.tools), "calculator": calculator, "version": task.version}
        record = {"fixture_id": task.id, "fixture_version": task.version,
            "input_hash": hashlib.sha256(json.dumps(request_material, sort_keys=True, allow_nan=False).encode()).hexdigest(),
            "mode": mode, "evidence": "synthetic" if mode == "fixture" else "reported-observation",
            "requested_provider": provider, "requested_model": model, "actual_provider": None, "actual_model": None,
            "status": "not_started", "duration_ms": None, "provider_usage": None, "estimated_cost_usd": None,
            "pricing_source": None, "response_status": None, "parse_status": "not_attempted", "scores": None,
            "proposed_tools": [], "answer": None, "proposal": None, "error": None}
        started = self.clock()
        phase = "provider" if mode == "provider" else "parse"
        try:
            if mode == "fixture":
                if type(fixture) is not dict or not isinstance(fixture.get("answer"), str):
                    record["status"] = "fixture_missing"
                    return record
                answer = fixture["answer"]
                record["response_status"] = "supplied_fixture"
                # Explicit fixture calls test the scorer; they are never observed provider calls.
                calls = fixture.get("proposed_tools", [])
                if type(calls) is not list or any(type(c) is not str for c in calls):
                    raise ValueError("fixture proposed_tools must be a list of names")
            else:
                adapter = ADAPTERS[provider]()
                instruction = ("Evaluate this synthetic task. Do not perform actions. Return the requested JSON shape. "
                    "Tools, if requested, are proposals only and will not be executed. Use only supplied inputs; disclose missing data.")
                if calculator:
                    instruction += (" Return entries and a calculation string in the bounded Decimal assignment grammar. "
                        "Assign the complete metrics dictionary to result. No imports, attributes, loops or functions. "
                        "Context data is available as context. Allowed functions: Decimal, abs, sum, min, max, round, mean.")
                request = adapter.build_request(context=_context(), system_prompt=instruction,
                    messages=[{"role": "user", "content": json.dumps(public, sort_keys=True, allow_nan=False)}],
                    tools=[_tool(schema) for schema in task.tools])
                response = (self.client or SDKClient()).invoke(provider=provider, model=model, request=request,
                    max_output_tokens=self.max_output_tokens, timeout_seconds=self.timeout_seconds)
                record["actual_provider"] = adapter.provider
                record["actual_model"] = getattr(response, "model", None) or getattr(response, "model_version", None)
                record["response_status"] = adapter.terminal_reason(response)
                if _usage_present(response, provider):
                    usage = adapter.extract_usage(response)
                    record["provider_usage"] = usage
                    cost = estimate_model_cost_usd(record["actual_model"], usage)
                    record["estimated_cost_usd"] = str(cost) if cost is not None else None
                    record["pricing_source"] = PRICING_SOURCE if cost is not None else None
                if record["response_status"] not in {"end_turn", "tool_use"}:
                    record["status"] = record["response_status"]
                    return record
                answer = adapter.final_response(response)
                calls = [call["name"] for call in adapter.parse_tool_calls(response)]
            phase = "parse"
            if len(answer.encode()) > MAX_RESPONSE_BYTES:
                record["status"] = "response_too_large"
                return record
            record["answer"] = answer
            record["proposed_tools"] = calls
            if task.oracle is not None:
                value = strict_json_loads(answer)
                if type(value) is not dict or "entries" not in value or ("metrics" not in value and not calculator):
                    raise ValueError("entries and metrics are required")
                if calculator:
                    result = FinancialCalculator().evaluate(value.get("calculation"), context={"context": task.context})
                    if not result.success or type(result.result) is not dict:
                        record["status"] = "calculation_failed"
                        record["error"] = result.error or "Calculation must return a metrics object"
                        return record
                    value["metrics"] = result.result
                verification = task.oracle.verify_proposal(value["entries"], value.get("metrics"))
                record["proposal"] = value
                record["scores"] = {"oracle": asdict(verification)}
            else:
                fixture_definition = task.lexical_fixture
                lower = answer.lower()
                terms = fixture_definition.expected_terms if fixture_definition else []
                expected_tools = fixture_definition.expected_tools if fixture_definition else []
                record["scores"] = {
                    "lexical_term_coverage": sum(t.lower() in lower for t in terms) / len(terms) if terms else None,
                    "proposed_tool_coverage": len(set(calls) & set(expected_tools)) / len(set(expected_tools)) if expected_tools else None,
                    "financial_accuracy": None, "execution_control_enforcement": None,
                }
            record["parse_status"] = "parsed"
            record["status"] = "completed"
        except (ValueError, TypeError, KeyError) as exc:
            record["status"] = "malformed_output" if phase == "parse" else "provider_error"
            record["parse_status"] = "failed" if phase == "parse" else "not_attempted"
            record["error"] = type(exc).__name__
        except Exception as exc:
            error_name = type(exc).__name__
            status_code = getattr(exc, "status_code", None)
            record["status"] = ("timeout" if "timeout" in error_name.lower() else
                "rate_limited" if status_code == 429 else "authentication_error" if status_code in {401, 403} else "provider_error")
            record["error"] = error_name
        finally:
            # Fixture timings describe this scorer only, never provider performance.
            record["duration_ms"] = round(max(0, self.clock() - started) * 1000, 3)
        return record


def summarize(records: list[dict]) -> dict:
    return {"requested_runs": len(records), "completed_runs": sum(r["status"] == "completed" for r in records),
        "failed_runs": sum(r["status"] != "completed" for r in records),
        "oracle_passed_runs": sum(bool((r.get("scores") or {}).get("oracle", {}).get("passed")) for r in records),
        "usage_available_runs": sum(r["provider_usage"] is not None for r in records)}


def compare_records(left: dict, right: dict) -> dict:
    """Comparison of two separately collected answers, never factual corroboration."""
    from ..runtime.consensus_engine import DualModelConsensusEngine
    result = {"fixture_id": left["fixture_id"], "models": [left["requested_model"], right["requested_model"]],
        "status": "unavailable", "agreement_score": None, "independent_factual_corroboration": False}
    if left["status"] != "completed" or right["status"] != "completed" or left["proposal"] is None or right["proposal"] is None:
        return result
    # Only the answer is compared: a calculation string is working the calculator
    # already turned into ``metrics``, so two routes to the same metrics agree.
    def answer(proposal):
        return {key: proposal.get(key) for key in ("entries", "metrics")}
    try:
        comparison = DualModelConsensusEngine().compare_proposals(
            model_a_name=left["requested_model"] or "fixture-a", output_a=answer(left["proposal"]),
            model_b_name=right["requested_model"] or "fixture-b", output_b=answer(right["proposal"]))
        from decimal import Decimal
        def normalized(proposal):
            return (
                sorted((entry["account_number"], entry.get("entity_id") or "", Decimal(str(entry["debit"])), Decimal(str(entry["credit"]))) for entry in proposal["entries"]),
                {key: Decimal(str(value)) for key, value in proposal["metrics"].items()},
            )
        exact_match = normalized(left["proposal"]) == normalized(right["proposal"])
        result.update(status=comparison.status.value if exact_match else "DIVERGENT",
            component_status=comparison.status.value, agreement_score=comparison.agreement_score,
            exact_account_entity_amount_metric_match=exact_match,
            discrepancies=[str(d.explanation) for d in comparison.discrepancies])
        if not exact_match and not result["discrepancies"]:
            result["discrepancies"] = ["Exact account, entity, amount or metric values differ."]
    except Exception as exc:
        result.update(status="comparison_error", error=type(exc).__name__)
    return result


def parse_targets(values: str) -> list[tuple[str, str]]:
    targets = []
    for value in values.split(","):
        provider, separator, model = value.strip().partition(":")
        if not separator or provider not in ADAPTERS or not model:
            raise ValueError("Models must use supported provider:model syntax")
        targets.append((provider, model))
    if len(set(targets)) != len(targets):
        raise ValueError("Duplicate provider/model target")
    return targets


def markdown_report(title: str, records: list[dict]) -> str:
    summary = summarize(records)
    mode_label = "synthetic fixture scoring" if all(r["mode"] == "fixture" for r in records) else "provider observations on synthetic tasks"
    lines = [f"# {title}", "", f"Mode: {mode_label}. Proposed tools are never executed.", "",
        f"Requested: {summary['requested_runs']}; completed: {summary['completed_runs']}; failed: {summary['failed_runs']}; oracle passes: {summary['oracle_passed_runs']}.", "",
        "| Task | Requested model | Actual model | Status | Scorer/provider duration ms | Usage | Estimated USD | Scores |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for row in records:
        cells = [row["fixture_id"], row["requested_model"] or "fixture", row["actual_model"] or "unavailable", row["status"],
            row["duration_ms"], row["provider_usage"] if row["provider_usage"] is not None else "unavailable",
            row["estimated_cost_usd"] if row["estimated_cost_usd"] is not None else "unavailable", row["scores"] or "unavailable"]
        lines.append("| " + " | ".join(str(v).replace("|", "\\|").replace("\n", " ") for v in cells) + " |")
    lines += ["", "Costs, when available, are estimates from provider-reported usage and the versioned repository pricing table recorded per run; they are not billing quotes.",
        "Fixture scorer duration is not provider latency. Failures remain in all completion denominators. No model ranking, representative accuracy, or live financial-control assurance is established by this report."]
    return "\n".join(lines)

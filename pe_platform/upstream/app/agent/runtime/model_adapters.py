from __future__ import annotations

import json
from typing import Any, Protocol

from ...services.redaction import redact_to_string
from .types import ContextPacket, ToolDefinition


class ModelAdapter(Protocol):
    provider: str

    def build_request(
        self,
        *,
        context: ContextPacket,
        system_prompt: str,
        messages: list[dict[str, Any]],
        tools: list[ToolDefinition],
    ) -> dict[str, Any]:
        ...

    def parse_tool_calls(self, response: Any) -> list[dict[str, Any]]:
        ...

    def submit_tool_results(self, messages: list[dict[str, Any]], tool_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        ...

    def stream_response(self, **kwargs: Any) -> Any:
        ...

    def final_response(self, response: Any) -> str:
        ...

    def extract_usage(self, response: Any) -> dict[str, int]:
        ...

    def normalize_errors(self, error: Exception) -> str:
        ...

    def terminal_reason(self, response: Any) -> str:  # CRAFT-OK: dynamic provider SDK response object boundary
        ...

    def supports_temperature(self, model: str) -> bool:
        ...


# PLAN-EXEMPT: plan 20260711-agentic-workflow-hardening A2A3 — provider seam: normalized terminal
# reason (AP-LAO-02) + per-model temperature capability (AP-LAO-01). The loop must BRANCH on why a
# turn ended (a max_tokens turn is truncated, not final) and must not send temperature to model
# classes that reject it. Both live in the adapter because the raw vocabulary is provider-specific.
TERMINAL_TOOL_USE = "tool_use"
TERMINAL_END_TURN = "end_turn"
TERMINAL_MAX_TOKENS = "max_tokens"
TERMINAL_PAUSE_TURN = "pause_turn"
TERMINAL_REFUSAL = "refusal"
TERMINAL_UNKNOWN = "unknown"

# OpenAI and reasoning model-id prefixes that reject temperature != 1. Conservative,
# prefix-matched; extend in the same edit that routes a new reasoning model.
OPENAI_REASONING_MODEL_PREFIXES: tuple[str, ...] = ("o1", "o3", "o4", "gpt-5", "deepseek-r1", "deepseek-reasoner")


def _normalize_terminal_reason(raw: str | None) -> str:
    value = (raw or "").strip().lower()
    if not value:
        return TERMINAL_UNKNOWN
    if value in {"tool_use", "tool_calls", "function_call", "function_calls"}:
        return TERMINAL_TOOL_USE
    if value in {"end_turn", "stop", "stop_sequence", "finishreason.stop"}:
        return TERMINAL_END_TURN
    if value in {"max_tokens", "length", "finishreason.max_tokens"}:
        return TERMINAL_MAX_TOKENS
    if value == "pause_turn":
        return TERMINAL_PAUSE_TURN
    if value in {
        "refusal",
        "content_filter",
        "sensitive",
        "safety",
        "recitation",
        "blocklist",
        "prohibited_content",
        "finishreason.safety",
        "finishreason.recitation",
        "finishreason.blocklist",
    }:
        return TERMINAL_REFUSAL
    return f"other:{value}"


class AnthropicMessagesAdapter:
    provider = "anthropic"

    def build_request(
        self,
        *,
        context: ContextPacket,
        system_prompt: str,
        messages: list[dict[str, Any]],
        tools: list[ToolDefinition],
    ) -> dict[str, Any]:
        return {
            "system": system_prompt,
            "messages": messages,
            "tools": [tool.to_model_schema() for tool in tools],
            "metadata": {"agent_run_id": context.run_id, "company_id": context.company_id},
        }

    def parse_tool_calls(self, response: Any) -> list[dict[str, Any]]:
        calls = []
        for block in getattr(response, "content", []) or []:
            if getattr(block, "type", None) == "tool_use":
                calls.append({"id": block.id, "name": block.name, "input": block.input, "raw": block})
        return calls

    def submit_tool_results(self, messages: list[dict[str, Any]], tool_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        messages.append({"role": "user", "content": tool_results})
        return messages

    def stream_response(self, client: Any, **create_kwargs: Any) -> Any:
        """Open an Anthropic streaming context (audit S2).

        Returns the context manager produced by ``client.messages.stream(...)``.
        The caller is responsible for entering it and iterating
        ``text_stream`` / events, then calling ``get_final_message()`` for the
        completed message (used to capture tool calls and usage). This is only
        used for the FINAL answer turn; tool-calling turns stay non-streamed
        because intermediate turns need the full tool_use content block.
        """
        return client.messages.stream(**create_kwargs)

    def iter_text_deltas(self, stream_manager: Any) -> Any:
        """Yield real text deltas from an open Anthropic stream context.

        Prefers the SDK's ``text_stream`` helper; falls back to raw events and
        extracts ``content_block_delta`` text deltas so lightweight test doubles
        only need to emit event objects.
        """
        text_stream = getattr(stream_manager, "text_stream", None)
        if text_stream is not None:
            for delta in text_stream:
                if delta:
                    yield delta
            return
        for event in stream_manager:
            event_type = getattr(event, "type", None)
            if event_type == "content_block_delta":
                delta = getattr(event, "delta", None)
                text = getattr(delta, "text", None)
                if text:
                    yield text

    def final_response(self, response: Any) -> str:
        answer = ""
        for block in getattr(response, "content", []) or []:
            text = getattr(block, "text", None)
            if text:
                answer += text
        return answer

    def extract_usage(self, response: Any) -> dict[str, int]:
        usage = getattr(response, "usage", None)
        if not usage:
            return {"input_tokens": 0, "output_tokens": 0}
        return {
            "input_tokens": int(getattr(usage, "input_tokens", 0) or 0),
            "output_tokens": int(getattr(usage, "output_tokens", 0) or 0),
        }

    def normalize_errors(self, error: Exception) -> str:
        error_detail = redact_to_string(str(error), max_length=500)
        response = getattr(error, "response", None)
        if response is not None:
            try:
                error_detail += f" - Response: {redact_to_string(response.text, max_length=500)}"
            except Exception:
                pass
        return error_detail

    # PLAN-EXEMPT: plan 20260711-agentic-workflow-hardening A2A3 — AP-LAO-02/01 adapter half.
    def terminal_reason(self, response: Any) -> str:  # CRAFT-OK: dynamic provider SDK response object boundary
        # LLMLOOP-OK: adapter-side normalizer, not a final-answer site — the loop branches this
        # normalized value (max_tokens→truncation disclosure, pause_turn→continue, refusal→safe refusal).
        return _normalize_terminal_reason(getattr(response, "stop_reason", None))

    def supports_temperature(self, model: str) -> bool:
        # Standard (non-extended-thinking) Claude accepts temperature 0-1. Vesper Bot does not
        # enable extended thinking on this lane; revisit in the same edit that ever does.
        return True


class OpenAIChatAdapter:
    provider = "openai"

    def build_request(
        self,
        *,
        context: ContextPacket,
        system_prompt: str,
        messages: list[dict[str, Any]],
        tools: list[ToolDefinition],
    ) -> dict[str, Any]:
        return {
            "messages": [{"role": "system", "content": system_prompt}, *messages],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.input_schema,
                    },
                }
                for tool in tools
            ],
            "metadata": {
                "agent_run_id": context.run_id,
                "company_id": context.company_id,
                "organization_id": context.organization_id,
                "organization_company_id": context.organization_company_id,
                "qbo_realm_id": context.qbo_realm_id,
            },
        }

    def parse_tool_calls(self, response: Any) -> list[dict[str, Any]]:
        message = self._message(response)
        calls = []
        for call in getattr(message, "tool_calls", []) or []:
            function = getattr(call, "function", None)
            name = getattr(function, "name", None)
            arguments = getattr(function, "arguments", "{}")
            try:
                tool_input = json.loads(arguments or "{}") if isinstance(arguments, str) else (arguments or {})
            except json.JSONDecodeError:
                tool_input = {"_raw_arguments": arguments}
            if name:
                calls.append({"id": getattr(call, "id", None), "name": name, "input": tool_input, "raw": call})
        return calls

    def submit_tool_results(self, messages: list[dict[str, Any]], tool_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        for result in tool_results:
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": result.get("tool_call_id") or result.get("tool_use_id") or result.get("id"),
                    "content": result.get("content") or result.get("result") or "",
                }
            )
        return messages

    def stream_response(self, client: Any, **create_kwargs: Any) -> Any:
        return client.chat.completions.create(stream=True, **create_kwargs)

    def final_response(self, response: Any) -> str:
        message = self._message(response)
        return getattr(message, "content", None) or ""

    def extract_usage(self, response: Any) -> dict[str, int]:
        usage = getattr(response, "usage", None)
        if not usage:
            return {"input_tokens": 0, "output_tokens": 0}
        input_tokens = int(getattr(usage, "prompt_tokens", 0) or getattr(usage, "input_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "completion_tokens", 0) or getattr(usage, "output_tokens", 0) or 0)
        details = getattr(usage, "prompt_tokens_details", None)
        cached = int(getattr(details, "cached_tokens", 0) or 0) if details is not None else 0
        payload = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": int(getattr(usage, "total_tokens", input_tokens + output_tokens) or 0),
        }
        if cached:
            payload["cached_input_tokens"] = cached
        return payload

    def normalize_errors(self, error: Exception) -> str:
        error_detail = redact_to_string(str(error), max_length=500)
        response = getattr(error, "response", None)
        if response is not None:
            try:
                error_detail += f" - Response: {redact_to_string(response.text, max_length=500)}"
            except Exception:
                pass
        return error_detail

    # PLAN-EXEMPT: plan 20260711-agentic-workflow-hardening A2A3 — AP-LAO-02/01 adapter half.
    def terminal_reason(self, response: Any) -> str:  # CRAFT-OK: dynamic provider SDK response object boundary
        # LLMLOOP-OK: adapter-side normalizer, not a final-answer site — the loop branches this
        # normalized value (max_tokens→truncation disclosure, pause_turn→continue, refusal→safe refusal).
        choices = getattr(response, "choices", []) or []
        raw = getattr(choices[0], "finish_reason", None) if choices else None
        return _normalize_terminal_reason(raw)

    def supports_temperature(self, model: str) -> bool:
        normalized = (model or "").strip().lower()
        return not normalized.startswith(OPENAI_REASONING_MODEL_PREFIXES)

    @staticmethod
    def _message(response: Any) -> Any:
        choices = getattr(response, "choices", []) or []
        if not choices:
            return None
        return getattr(choices[0], "message", None)


class GoogleGeminiAdapter:
    provider = "google"

    def build_request(
        self,
        *,
        context: ContextPacket,
        system_prompt: str,
        messages: list[dict[str, Any]],
        tools: list[ToolDefinition],
    ) -> dict[str, Any]:
        gemini_tools = []
        if tools:
            declarations = []
            for tool in tools:
                schema = tool.input_schema.copy() if hasattr(tool.input_schema, "copy") else dict(tool.input_schema)
                declarations.append({
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": schema,
                })
            gemini_tools = [{"function_declarations": declarations}]

        contents = []
        for msg in messages:
            role = "model" if msg.get("role") in {"assistant", "model"} else "user"
            content = msg.get("content")
            parts: list[dict[str, Any]] = []
            if isinstance(content, str):
                parts.append({"text": content})
            elif isinstance(content, list):
                for item in content:
                    if isinstance(item, dict):
                        if item.get("type") == "tool_use":
                            parts.append({
                                "function_call": {
                                    "name": item.get("name"),
                                    "args": item.get("input", {}),
                                }
                            })
                        elif "function_call" in item:
                            parts.append(item)
                        elif "function_response" in item:
                            parts.append(item)
                        elif item.get("role") == "tool" or item.get("tool_use_id") or item.get("tool_call_id"):
                            parts.append({
                                "function_response": {
                                    "name": item.get("name", "tool"),
                                    "response": {"result": item.get("content", "")},
                                }
                            })
                        elif "text" in item:
                            parts.append({"text": item["text"]})
                    elif isinstance(item, str):
                        parts.append({"text": item})
            elif isinstance(content, dict):
                parts.append(content)
            if parts:
                contents.append({"role": role, "parts": parts})

        return {
            "system_instruction": system_prompt,
            "contents": contents,
            "tools": gemini_tools,
            "metadata": {
                "agent_run_id": context.run_id,
                "company_id": context.company_id,
                "organization_id": context.organization_id,
                "organization_company_id": context.organization_company_id,
                "qbo_realm_id": context.qbo_realm_id,
            },
        }

    def parse_tool_calls(self, response: Any) -> list[dict[str, Any]]:
        calls = []
        candidates = getattr(response, "candidates", []) or []
        for cand in candidates:
            content = getattr(cand, "content", None) or {}
            parts = getattr(content, "parts", []) if hasattr(content, "parts") else (content.get("parts", []) if isinstance(content, dict) else [])
            for part in parts or []:
                fn_call = getattr(part, "function_call", None) or (part.get("function_call") if isinstance(part, dict) else None)
                if fn_call:
                    name = getattr(fn_call, "name", None) or (fn_call.get("name") if isinstance(fn_call, dict) else None)
                    args = getattr(fn_call, "args", {}) or (fn_call.get("args") if isinstance(fn_call, dict) else {})
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except Exception:
                            args = {"_raw_arguments": args}
                    elif not isinstance(args, dict):
                        try:
                            args = dict(args)
                        except Exception:
                            args = {"_raw_arguments": str(args)}
                    call_id = getattr(fn_call, "id", None) or f"call_{name}_{len(calls) + 1}"
                    if name:
                        calls.append({"id": call_id, "name": name, "input": args, "raw": part})
        return calls

    def submit_tool_results(self, messages: list[dict[str, Any]], tool_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        parts = []
        for result in tool_results:
            call_id = result.get("tool_call_id") or result.get("tool_use_id") or result.get("id") or "call"
            name = result.get("name") or "tool"
            content = result.get("content") or result.get("result") or ""
            parts.append({
                "function_response": {
                    "name": name,
                    "response": {"name": name, "content": content, "tool_call_id": call_id},
                }
            })
        messages.append({"role": "user", "parts": parts, "content": parts})
        return messages

    def stream_response(self, client: Any, **create_kwargs: Any) -> Any:
        if hasattr(client, "models") and hasattr(client.models, "generate_content_stream"):
            return client.models.generate_content_stream(**create_kwargs)
        if hasattr(client, "generate_content"):
            return client.generate_content(stream=True, **create_kwargs)
        return client.chat.completions.create(stream=True, **create_kwargs)

    def iter_text_deltas(self, stream_manager: Any) -> Any:
        for chunk in stream_manager:
            text = getattr(chunk, "text", None)
            if text:
                yield text

    def final_response(self, response: Any) -> str:
        text = getattr(response, "text", None)
        if text:
            return text
        candidates = getattr(response, "candidates", []) or []
        for cand in candidates:
            content = getattr(cand, "content", None) or {}
            parts = getattr(content, "parts", []) if hasattr(content, "parts") else (content.get("parts", []) if isinstance(content, dict) else [])
            for part in parts or []:
                t = getattr(part, "text", None) or (part.get("text") if isinstance(part, dict) else None)
                if t:
                    return t
        return ""

    def extract_usage(self, response: Any) -> dict[str, int]:
        usage = getattr(response, "usage_metadata", None)
        if not usage:
            return {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        prompt = int(getattr(usage, "prompt_token_count", 0) or 0)
        candidates = int(getattr(usage, "candidates_token_count", 0) or 0)
        total = int(getattr(usage, "total_token_count", prompt + candidates) or (prompt + candidates))
        cached = int(getattr(usage, "cached_content_token_count", 0) or 0)
        payload = {
            "input_tokens": prompt,
            "output_tokens": candidates,
            "total_tokens": total,
        }
        if cached:
            payload["cached_input_tokens"] = cached
        return payload

    def normalize_errors(self, error: Exception) -> str:
        return redact_to_string(str(error), max_length=500)

    def terminal_reason(self, response: Any) -> str:
        candidates = getattr(response, "candidates", []) or []
        if not candidates:
            return TERMINAL_UNKNOWN
        cand = candidates[0]
        content = getattr(cand, "content", None) or {}
        parts = getattr(content, "parts", []) if hasattr(content, "parts") else (content.get("parts", []) if isinstance(content, dict) else [])
        for part in parts or []:
            if getattr(part, "function_call", None) or (isinstance(part, dict) and "function_call" in part):
                return TERMINAL_TOOL_USE
        raw = getattr(cand, "finish_reason", None)
        if raw is not None and not isinstance(raw, str):
            raw = getattr(raw, "name", str(raw))
        return _normalize_terminal_reason(raw)

    def supports_temperature(self, model: str) -> bool:
        return True


class OpenAICompatibleAdapter(OpenAIChatAdapter):
    provider = "openai_compatible"

    def __init__(self, provider_name: str = "openai_compatible") -> None:
        self.provider = provider_name

    def supports_temperature(self, model: str) -> bool:
        normalized = (model or "").strip().lower()
        if "reasoner" in normalized or "r1" in normalized:
            return False
        return super().supports_temperature(model)


def adapter_for_provider(provider: str) -> ModelAdapter:
    normalized = (provider or "").strip().lower()
    if normalized in {"openai", "gpt"}:
        return OpenAIChatAdapter()
    if normalized in {"anthropic", "claude", ""}:
        return AnthropicMessagesAdapter()
    if normalized in {"google", "gemini"}:
        return GoogleGeminiAdapter()
    if normalized in {"openai_compatible", "deepseek", "groq", "ollama", "vllm", "together"}:
        return OpenAICompatibleAdapter(provider_name=normalized)
    raise ValueError(
        f"no ModelAdapter for provider '{provider}' — supported: anthropic/claude, openai/gpt, google/gemini, openai_compatible. "
        "Adding a provider means a full adapter (parse/submit/stream/final/usage/errors/"
        "terminal_reason/supports_temperature), an adapter_for_provider row, and pricing rows."
    )

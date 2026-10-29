"""Canonical runtime exports, loaded only when a caller requests them.

Pure calculator, adapter and evaluation imports need no application runtime.
Explicitly requesting an application export retains its original object identity.
"""

from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .context_packet import ContextPacketBuilder, PreparedContextSources
    from .model_loop import AgentLoopResult, AgentStreamEvent, AgentStreamState, AnthropicRuntimeLoop
    from .prompt_builder import PromptBuilder
    from .runtime import AgentRuntime, AgentRuntimeSession
    from .tool_executor import ToolExecutor
    from .tool_policy import ToolPolicyGate, ToolPolicyViolation
    from .tool_registry import ToolRegistry, build_assistant_tool_registry
    from .types import AgentIntent, OutputMode, RiskLevel, ToolDefinition

__all__ = [
    "AgentIntent",
    "AgentRuntime",
    "AgentRuntimeSession",
    "AgentLoopResult",
    "AgentStreamEvent",
    "AgentStreamState",
    "AnthropicRuntimeLoop",
    "ContextPacketBuilder",
    "OutputMode",
    "PreparedContextSources",
    "PromptBuilder",
    "RiskLevel",
    "ToolDefinition",
    "ToolExecutor",
    "ToolPolicyGate",
    "ToolPolicyViolation",
    "ToolRegistry",
    "build_assistant_tool_registry",
]

_EXPORT_MODULES = {
    "AgentIntent": ".types",
    "AgentRuntime": ".runtime",
    "AgentRuntimeSession": ".runtime",
    "AgentLoopResult": ".model_loop",
    "AgentStreamEvent": ".model_loop",
    "AgentStreamState": ".model_loop",
    "AnthropicRuntimeLoop": ".model_loop",
    "ContextPacketBuilder": ".context_packet",
    "OutputMode": ".types",
    "PreparedContextSources": ".context_packet",
    "PromptBuilder": ".prompt_builder",
    "RiskLevel": ".types",
    "ToolDefinition": ".types",
    "ToolExecutor": ".tool_executor",
    "ToolPolicyGate": ".tool_policy",
    "ToolPolicyViolation": ".tool_policy",
    "ToolRegistry": ".tool_registry",
    "build_assistant_tool_registry": ".tool_registry",
}


def __getattr__(name: str):
    module_name = _EXPORT_MODULES.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module_name, __name__), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))

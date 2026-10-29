"""Structured untrusted-content boundaries for the LLM prompt (C3).

Wraps proactively-injected untrusted free-text (retrieved documents, company
memory, financial context) in a spoof-resistant ``<untrusted>`` fence so the model
can reliably tell where untrusted data begins and ends. Paired with a system
instruction (``prompt_builder.build_stable_policy``) that tells the model to treat
fenced content strictly as data.

DEFENSE-IN-DEPTH, NOT injection-proof: per the SoK arXiv 2601.17548, structured
boundaries REDUCE the prompt-injection surface but do not eliminate it (85%+
residual attack-success vs state-of-the-art defenses). The PRIMARY controls are
architectural — proposal-first writes, fingerprint-bound confirmation tokens, and
the scope-strip on tool args (see ``.claude/rules/agentic-writeback.md``). This is
a hardening layer on top of those, not a replacement for them.
"""
from __future__ import annotations

import re

FENCE_TAG = "untrusted"

# Matches ANY open/close structural tag a hostile body could use to break out of — or
# spoof — the fence: the ``<untrusted …>`` boundary itself PLUS instruction-role / tool
# tags (``<system>`` / ``<developer>`` / ``<tool_result>`` / ``<function_calls>`` /
# ``<function_results>``). Case-insensitive + whitespace-tolerant so ``</UNTRUSTED>`` /
# ``</ untrusted >`` (which a naive literal replace would miss) are caught. Each match is
# replaced with an inert literal — the body carries the TEXT as data, never a live tag.
_NEUTRALIZE_TAG_RE = re.compile(
    r"<\s*/?\s*(?:untrusted|system|developer|tool_result|tool_results|function_calls|function_results)\b[^>]*>",
    re.IGNORECASE,
)

# The source label is attacker-controllable (e.g. ``block["type"]``); allowlist it so
# it cannot inject into the opening tag's attribute value.
_SOURCE_RE = re.compile(r"[^a-z0-9_-]+")


def _sanitize_source(source: str) -> str:
    cleaned = _SOURCE_RE.sub("-", (source or "context").strip().lower()).strip("-")
    return cleaned or "context"


# PLAN-EXEMPT: plan 20260711-agentic-workflow-hardening B4 — two helpers closing the D2 fence
# gaps: G1 (a tenant-controlled string interpolated into the TRUSTED system prompt) and
# G2/G3 (raw untrusted text reaching lane-LLM prompts / user-visible fallback answers bare).
def neutralize_untrusted_text(content: str) -> str:
    """Neutralize structural/instruction-role tags in untrusted text WITHOUT fencing it.

    For surfaces where a full ``<untrusted>`` fence is wrong (user-visible fallback text,
    telemetry strings) but a live ``</untrusted>`` / ``<system>`` tag inside the content
    must still be defused.
    """
    text = content if isinstance(content, str) else str(content)
    return _NEUTRALIZE_TAG_RE.sub("[fenced-tag]", text)


def sanitize_inline_value(value: str, *, max_length: int = 120) -> str:
    """Sanitize a short untrusted scalar (a QBO-synced company name) for interpolation
    into TRUSTED prompt text: tags neutralized, newlines collapsed, length clamped.
    Keeps the trusted channel's byte-stability property (a stable input stays stable)."""
    text = value if isinstance(value, str) else str(value or "")
    text = _NEUTRALIZE_TAG_RE.sub("[fenced-tag]", text)
    text = " ".join(text.split())
    return text[:max_length] if text else "this company"


def wrap_untrusted(source: str, content: str) -> str:
    """Fence untrusted ``content`` in a spoof-resistant ``<untrusted>`` boundary.

    ALWAYS neutralizes embedded fence / instruction-role / tool tags (any ``<untrusted …>``
    / ``<system>`` / tool-tag variant) to an inert literal and wraps the result at a
    CODE-controlled boundary — it never trusts the content's prefix. A prior version
    short-circuited when the content already started with ``<untrusted`` ("already fenced");
    that trusted attacker-controllable text and let a body that BEGINS with
    ``<untrusted>…</untrusted>`` followed by injected instruction-like text break out of its
    fence (D3). ``wrap_untrusted`` has a SINGLE call site (``prompt_builder._render_blocks``)
    that always passes RAW untrusted content, so always-neutralize-and-wrap cannot
    double-fence trusted content. ``source`` is sanitized to an allowlisted label.

    DEFENSE-IN-DEPTH, not injection-proof (see the module docstring / SoK arXiv 2601.17548).
    """
    text = content if isinstance(content, str) else str(content)
    safe_source = _sanitize_source(source)
    neutralized = _NEUTRALIZE_TAG_RE.sub("[fenced-tag]", text)
    return f'<untrusted source="{safe_source}">\n{neutralized}\n</untrusted>'

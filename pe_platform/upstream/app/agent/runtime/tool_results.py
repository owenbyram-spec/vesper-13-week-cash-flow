# PLAN-EXEMPT: ratified plan 20260713-wave2-6-tool-hardening chunk C4 (marker
# RATIFICATION-2026-07-13.md on disk; active-plan.json sidecar absent in this
# dispatched session — standing precedent). Decision 5 / AC-6: typed tool results.
"""Typed tool results — plan 20260713-wave2-6-tool-hardening C4 (decision 5 / AC-6).

Pydantic models for the assistant's JSON-read result class, typed EXACTLY where
the result has a real consumer (the tool_metadata no-consumer doctrine,
``app/agent/runtime/tool_metadata.py``: a metadata/typing surface without a
consumer is decorative and forbidden):

- ``UnmatchedItemsPage`` — ``list_unmatched_items``
  (``handlers._handle_list_unmatched_items``); the wire output IS
  ``serialize_result(model)``.
- ``ReconTieoutResult`` — ``get_recon_tieout``
  (``handlers._handle_get_recon_tieout``); the wire output IS
  ``serialize_result(model)``.
- ``GLCursorPage`` — ``run_qbo_report``'s GL-cache cursor lane
  (``handlers._handle_run_qbo_report``): the page dict returned by
  ``qbo_gl_cache.query_gl_lines_page`` is validated here and the handler reads
  the TYPED page; that tool's wire format is prose rendered FROM this typed page
  (there is no JSON wire output on that lane), so for it the model is the typed
  seam + schema, not the serializer.

Byte-compat pin (decision 5 / E6): serialization is
``json.dumps(model.model_dump(mode="json"), default=str)`` — json.dumps DEFAULT
separators, which ``model_dump_json()`` does NOT use — with model field order
matching the pre-change dict insertion order, and Decimal-carried values typed
``str`` (constructed via ``str(...)`` exactly as before). ``extra="forbid"`` on
every model so producer shape drift fails LOUD instead of silently dropping a
new key from the wire output.
"""
from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_serializer


class _ForbidExtra(BaseModel):
    model_config = ConfigDict(extra="forbid")


def serialize_result(model: BaseModel) -> str:
    """The decision-5 serialization pin — byte-compatible with the pre-change
    ``json.dumps(<dict>, default=str)`` output (default separators + ensure_ascii;
    ``model_dump_json()`` would change the separators, which is why it is NOT used)."""
    return json.dumps(model.model_dump(mode="json"), default=str)


# ── list_unmatched_items ────────────────────────────────────────────────────────

class BankUnmatchedItem(_ForbidExtra):
    """A statement line with no book counterpart (``serialize_unmatched`` bank shape)."""

    bank_item_id: int
    amount: str | None  # Decimal-backed upstream; str(...) at construction (byte pin)
    date: str | None
    description: str
    why_not_matched: str
    why_detail: str | None


class BookUnmatchedItem(_ForbidExtra):
    """A book txn with no statement counterpart (``serialize_unmatched`` book shape)."""

    book_txn_id: int
    amount: str | None  # Decimal-backed upstream; str(...) at construction (byte pin)
    date: str | None
    description: str


class UnmatchedItemsPage(_ForbidExtra):
    """The ``list_unmatched_items`` page: ``agent_ops.serialize_unmatched`` output
    plus the handler-appended ``session_id``/``session_status`` (field order = the
    pre-change insertion order)."""

    side: str
    total_unmatched: int
    limit: int
    items: list[BankUnmatchedItem | BookUnmatchedItem]
    next_cursor: int | None
    has_more: bool
    session_id: int
    session_status: str | None


# ── get_recon_tieout ────────────────────────────────────────────────────────────

class TieoutSession(_ForbidExtra):
    """The session leg of the tie-out. ``anchor_note`` is conditionally present on
    the wire (only when no clean-start anchor is configured) — the wrap serializer
    omits it when None so the byte pin holds in both states."""

    session_id: int
    account_id: str
    account_name: str
    status: str | None
    period_start: str
    period_end: str
    opening: str  # Decimal-backed upstream; str(...) at construction (byte pin)
    anchor_configured: bool
    cleared_sum: str
    cleared_balance: str
    ending_balance: str
    difference: str
    tied_out: bool
    anchor_note: str | None = None

    @model_serializer(mode="wrap")
    def _omit_absent_anchor_note(self, handler):
        data = handler(self)
        if data.get("anchor_note") is None:
            data.pop("anchor_note", None)
        return data


class SubledgerSideTieout(_ForbidExtra):
    """A subledger side whose inputs loaded — the real tie-out numbers."""

    available: Literal[True]
    as_of: str
    subledger_total: float
    gl_control_balance: float
    break_amount: float
    tied_out: bool


class SubledgerSideUnavailable(_ForbidExtra):
    """Honest degradation: a side whose inputs could not be loaded — never a
    fabricated clean tie-out."""

    available: Literal[False]
    reason: str


class ReconTieoutResult(_ForbidExtra):
    """The ``get_recon_tieout`` payload (field order = the pre-change insertion
    order: session, subledger, note)."""

    session: TieoutSession
    subledger: dict[str, SubledgerSideTieout | SubledgerSideUnavailable]
    note: str


# ── run_qbo_report (GL-cache cursor lane) ───────────────────────────────────────

class GLTransactionLine(_ForbidExtra):
    """One cached GL register line (``query_gl_lines_page`` row shape)."""

    date: str
    type: str
    doc_num: str
    name: str
    memo: str
    split: str
    amount: float
    balance: float
    account_id: str | None
    account_name: str | None


class GLCursorPage(_ForbidExtra):
    """One keyset page over the cached GL register (``query_gl_lines_page`` return
    shape: window aggregates + continuation token). Consumed TYPED by
    ``_handle_run_qbo_report``, which renders prose from it."""

    transactions: list[GLTransactionLine]
    total_count: int
    total_amount: float
    limit: int | None
    next_cursor: str | None
    has_more: bool


# Exactly the tools whose results are typed above — the registry stamps each one's
# REAL ``model_json_schema()`` as its ``output_schema``; every other tool's
# output_schema is None (prose return, no typed consumer — the no-consumer doctrine).
TOOL_RESULT_MODELS: dict[str, type[BaseModel]] = {
    "list_unmatched_items": UnmatchedItemsPage,
    "get_recon_tieout": ReconTieoutResult,
    "run_qbo_report": GLCursorPage,
}

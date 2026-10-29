"""Bounded, cursor-driven LLM proposals for unmatched reconciliation rows.

The model is an untrusted proposer.  This module never clears a transaction,
creates a match, or writes to QBO.  It deterministically covers every plausible
candidate edge with bounded bank/book tiles, exposes at most forty unique ids
from each side to one provider call, and validates every returned id against
that exact batch with one-to-one proposal cardinality.

Continuation cursors are opaque, signed, and bound to the full authorized
tenant/company/realm triple, session, statement import, lineage state, complete
candidate-set hashes, and deterministic eligibility plan.  Every paid attempt
is identified without randomness in a dedicated FORCE-RLS relation.  Execution
atomically consumes ``claimed`` into ``started`` and commits before provider
spend; terminal results replay without spend.  Success requires the exact
foreign-keyed ``LLMUsageEvent`` returned by the provider wrapper.  One explicit
retry is available only after a durable, exactly metered provider failure.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Protocol

from sqlalchemy.orm import Session

from ...models import (
    BankReconciliationSuggestionAttempt,
    LLMUsageEvent,
    OrganizationCompany,
    ReconciliationSession,
)
from ..llm import LLMJSONCallResult, LaneConfig
from ..llm_task_context import bind_llm_task_context
from ..tenant_tag import bind_tenant_tag
from .match import UnmatchedBank, UnmatchedBook

MAX_CANDIDATES_PER_SIDE = 40
MAX_INPUT_TOKENS = 12_000
MAX_OUTPUT_TOKENS = 2_000
SDK_RETRIES = 0
PROVIDER_TIMEOUT_SECONDS = 30.0
NEAR_AMOUNT_ABSOLUTE_FLOOR = Decimal("0.50")
NEAR_AMOUNT_ABSOLUTE_CAP = Decimal("5.00")
NEAR_AMOUNT_RELATIVE_RATE = Decimal("0.01")
NEAR_AMOUNT_DATE_DAYS = 7

_DESCRIPTION_CHARS = 512
_INPUT_TOKEN_OVERHEAD = 256
_LLM_LANE = "analysis"
_TASK_TYPE = "other"  # llm_task_context.py has a closed task-type vocabulary.
_CURSOR_VERSION = 3
_CURSOR_MAX_CHARS = 4096
_PLAUSIBILITY_VERSION = "exact-or-near-dated-amount-or-distinctive-memo-v2"
_STARTED_STALE_AFTER = timedelta(seconds=60)
_TERMINAL_STATUSES = frozenset(
    {
        "succeeded",
        "provider_error",
        "provider_timeout",
        "usage_unmetered",
        "usage_ambiguous",
        "usage_mismatch",
        "invalid_response",
        "response_truncated",
        "result_unavailable",
    }
)
_HEX_32 = re.compile(r"[0-9a-f]{32}")
_HEX_64 = re.compile(r"[0-9a-f]{64}")
_MEMO_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)
_MEMO_STOPWORDS = frozenset(
    {
        "account",
        "bank",
        "card",
        "check",
        "credit",
        "debit",
        "deposit",
        "invoice",
        "online",
        "payment",
        "purchase",
        "transaction",
        "transfer",
    }
)


class _JsonLLM(Protocol):
    """Narrow provider-neutral seam used by this suggestion service."""

    @property
    def config(self) -> LaneConfig:
        ...

    def predict_json_result(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float = ...,
        model: str | None = ...,
        allow_fallback: bool = ...,
        task_type: str | None = ...,
        timeout_override: float | None = ...,
        max_retries_override: int | None = ...,
        max_output_tokens_override: int | None = ...,
        preallocated_usage_event_id: int | None = ...,
    ) -> LLMJSONCallResult:
        ...


class SuggestionCursorError(ValueError):
    """The continuation token is malformed, stale, or belongs to another scope."""


class SuggestionRetryRefused(ValueError):
    """An attempt is ineligible, indeterminate, or already claimed."""


class SuggestionRetryClaimNotCommitted(RuntimeError):
    """The write-ahead spend claim was not committed before provider execution."""


class SuggestionInputBudgetError(ValueError):
    """Even the minimum useful pair cannot fit the conservative input budget."""


@dataclass(frozen=True)
class SuggestionScope:
    """Server-derived identity to which every cursor and retry is bound."""

    realm_id: str
    session_id: int
    statement_import_id: int | None
    lineage_status: str
    organization_id: int | None = None
    organization_company_id: int | None = None


@dataclass(frozen=True)
class Suggestion:
    """One validated, detached, proposal-only bank-to-book pairing."""

    bank_item_id: int
    book_txn_id: int
    amount: Decimal | None
    confidence: float
    reason: str


@dataclass(frozen=True)
class SuggestResult:
    """One bounded suggestion page and its explicit continuation/retry choices."""

    suggestions: list[Suggestion]
    truncated: bool
    page_number: int
    planned_pages: int
    next_cursor: str | None = None
    has_more: bool = False
    retry_cursor: str | None = None
    failure_code: str | None = None


@dataclass(frozen=True)
class _CursorState:
    traversal_id: str
    page_index: int
    attempt: int
    bank_hash: str
    book_hash: str
    bank_count: int
    book_count: int
    eligibility_hash: str
    page_count: int


@dataclass(frozen=True)
class _CandidatePage:
    bank_rows: tuple[UnmatchedBank, ...]
    book_rows: tuple[UnmatchedBook, ...]
    user_prompt: str | None
    estimated_input_tokens: int


@dataclass(frozen=True)
class PreparedSuggestionBatch:
    """Validated provider input with a flushed claim or a durable cached result."""

    scope: SuggestionScope
    state: _CursorState
    bank_rows: tuple[UnmatchedBank, ...]
    book_rows: tuple[UnmatchedBook, ...]
    user_prompt: str | None
    estimated_input_tokens: int
    next_cursor: str | None
    has_more: bool
    current_step: str
    first_attempt_step: str
    page_fingerprint: str
    attempt_fingerprint: str
    user_id: int | None = None
    spend_claim_id: int | None = None
    cached_result: SuggestResult | None = None


@dataclass(frozen=True)
class _ResolvedSuggestionRoute:
    service: _JsonLLM
    provider: str
    model: str
    lane: str


@dataclass(frozen=True)
class _SpendAuthorization:
    usage_event_id: int


_SYSTEM_PROMPT = (
    "You are a bank-reconciliation assistant. You receive bounded lists of "
    "unmatched bank statement lines and unmatched book transactions from one "
    "authorized reconciliation session. Propose only pairs representing the "
    "same real-world transaction. Amounts should be equal or near-equal, dates "
    "close, and descriptions consistent. Returning no pairs is correct when "
    "nothing is plausible. Never invent or reuse an id outside the supplied "
    "lists. Respond only with JSON shaped as "
    '{"suggestions":[{"bank_item_id":<int>,"book_txn_id":<int>,'
    '"confidence":<0..1>,"reason":"<short>"}]}'
)


def _amount_text(value: Decimal | None) -> str | None:
    return format(value, "f") if value is not None else None


def _description(value: str | None) -> str:
    return (value or "")[:_DESCRIPTION_CHARS]


def _fmt_bank(rows: Sequence[UnmatchedBank]) -> list[dict[str, object]]:
    return [
        {
            "bank_item_id": row.bank_item_id,
            "amount": _amount_text(row.amount),
            "date": row.txn_date.isoformat() if row.txn_date else None,
            "description": _description(row.description),
        }
        for row in rows
    ]


def _fmt_book(rows: Sequence[UnmatchedBook]) -> list[dict[str, object]]:
    return [
        {
            "book_txn_id": row.book_txn_id,
            "amount": _amount_text(row.amount),
            "date": row.txn_date.isoformat() if row.txn_date else None,
            "description": _description(row.description),
        }
        for row in rows
    ]


def _build_user_prompt(
    bank_rows: Sequence[UnmatchedBank], book_rows: Sequence[UnmatchedBook]
) -> str:
    payload = {
        "unmatched_bank_lines": _fmt_bank(bank_rows),
        "unmatched_book_transactions": _fmt_book(book_rows),
    }
    return "Pair these unmatched items.\n" + json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def _estimated_input_tokens(user_prompt: str) -> int:
    """Conservative byte ceiling: byte-level tokenization cannot exceed bytes."""

    return (
        len(_SYSTEM_PROMPT.encode("utf-8"))
        + len(user_prompt.encode("utf-8"))
        + _INPUT_TOKEN_OVERHEAD
    )


def _candidate_key(
    amount: Decimal | None, txn_date: date | None, item_id: int
) -> tuple[bool, Decimal, bool, date, int]:
    # Amount-first bucketing puts likely equal-value pairs in the same bounded page.
    return (
        amount is None,
        amount if amount is not None else Decimal(0),
        txn_date is None,
        txn_date if txn_date is not None else date.max,
        item_id,
    )


def _ordered_bank(rows: Sequence[UnmatchedBank]) -> tuple[UnmatchedBank, ...]:
    ordered = tuple(
        sorted(
            rows,
            key=lambda row: _candidate_key(row.amount, row.txn_date, row.bank_item_id),
        )
    )
    ids = [row.bank_item_id for row in ordered]
    if len(ids) != len(set(ids)):
        raise SuggestionCursorError("The unmatched bank set contains duplicate ids.")
    return ordered


def _ordered_book(rows: Sequence[UnmatchedBook]) -> tuple[UnmatchedBook, ...]:
    ordered = tuple(
        sorted(
            rows,
            key=lambda row: _candidate_key(row.amount, row.txn_date, row.book_txn_id),
        )
    )
    ids = [row.book_txn_id for row in ordered]
    if len(ids) != len(set(ids)):
        raise SuggestionCursorError("The unmatched book set contains duplicate ids.")
    return ordered


def _rows_hash(rows: list[dict[str, object]]) -> str:
    canonical = json.dumps(
        rows, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    try:
        return base64.b64decode(value + padding, altchars=b"-_", validate=True)
    except (ValueError, TypeError) as exc:
        raise SuggestionCursorError("Invalid suggestion cursor.") from exc


def _cursor_key() -> bytes:
    from ...config import settings

    return hashlib.sha256(
        b"bank-reconciliation-suggestion-cursor-v3\0"
        + settings.secret_key.encode("utf-8")
    ).digest()


def _cursor_payload(scope: SuggestionScope, state: _CursorState) -> dict[str, object]:
    return {
        "v": _CURSOR_VERSION,
        "o": scope.organization_id,
        "c": scope.organization_company_id,
        "r": scope.realm_id,
        "s": scope.session_id,
        "i": scope.statement_import_id,
        "l": scope.lineage_status,
        "t": state.traversal_id,
        "p": state.page_index,
        "a": state.attempt,
        "bh": state.bank_hash,
        "kh": state.book_hash,
        "bc": state.bank_count,
        "kc": state.book_count,
        "eh": state.eligibility_hash,
        "pc": state.page_count,
    }


def _encode_cursor(scope: SuggestionScope, state: _CursorState) -> str:
    payload = json.dumps(
        _cursor_payload(scope, state), sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    encoded = _b64encode(payload)
    signature = _b64encode(hmac.new(_cursor_key(), payload, hashlib.sha256).digest())
    return f"{encoded}.{signature}"


def _require_int(payload: dict[str, object], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise SuggestionCursorError("Invalid suggestion cursor.")
    return value


def _decode_cursor(token: str, scope: SuggestionScope) -> _CursorState:
    if not token or len(token) > _CURSOR_MAX_CHARS or token.count(".") != 1:
        raise SuggestionCursorError("Invalid suggestion cursor.")
    encoded, encoded_signature = token.split(".", 1)
    payload_bytes = _b64decode(encoded)
    signature = _b64decode(encoded_signature)
    expected = hmac.new(_cursor_key(), payload_bytes, hashlib.sha256).digest()
    if not hmac.compare_digest(signature, expected):
        raise SuggestionCursorError("Invalid suggestion cursor.")
    try:
        payload = json.loads(payload_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise SuggestionCursorError("Invalid suggestion cursor.") from exc
    if not isinstance(payload, dict) or set(payload) != {
        "v",
        "o",
        "c",
        "r",
        "s",
        "i",
        "l",
        "t",
        "p",
        "a",
        "bh",
        "kh",
        "bc",
        "kc",
        "eh",
        "pc",
    }:
        raise SuggestionCursorError("Invalid suggestion cursor.")
    if (
        _require_int(payload, "v") != _CURSOR_VERSION
        or _require_int(payload, "o") != scope.organization_id
        or _require_int(payload, "c") != scope.organization_company_id
        or payload.get("r") != scope.realm_id
        or _require_int(payload, "s") != scope.session_id
        or payload.get("i") != scope.statement_import_id
        or payload.get("l") != scope.lineage_status
    ):
        raise SuggestionCursorError(
            "Suggestion cursor does not belong to this session."
        )
    traversal_id = payload.get("t")
    bank_hash = payload.get("bh")
    book_hash = payload.get("kh")
    eligibility_hash = payload.get("eh")
    if (
        not isinstance(traversal_id, str)
        or _HEX_32.fullmatch(traversal_id) is None
        or not isinstance(bank_hash, str)
        or _HEX_64.fullmatch(bank_hash) is None
        or not isinstance(book_hash, str)
        or _HEX_64.fullmatch(book_hash) is None
        or not isinstance(eligibility_hash, str)
        or _HEX_64.fullmatch(eligibility_hash) is None
    ):
        raise SuggestionCursorError("Invalid suggestion cursor.")
    state = _CursorState(
        traversal_id=traversal_id,
        page_index=_require_int(payload, "p"),
        attempt=_require_int(payload, "a"),
        bank_hash=bank_hash,
        book_hash=book_hash,
        bank_count=_require_int(payload, "bc"),
        book_count=_require_int(payload, "kc"),
        eligibility_hash=eligibility_hash,
        page_count=_require_int(payload, "pc"),
    )
    if state.attempt not in {0, 1}:
        raise SuggestionCursorError("Invalid suggestion cursor.")
    return state


def _memo_tokens(value: str | None) -> frozenset[str]:
    """Return stable memo identifiers strong enough to make a pair plausible."""

    normalized = _description(value).casefold()
    return frozenset(
        token
        for token in _MEMO_TOKEN.findall(normalized)
        if len(token) >= 4 and token not in _MEMO_STOPWORDS
    )


def _near_amount_and_date(bank: UnmatchedBank, book: UnmatchedBook) -> bool:
    if (
        bank.amount is None
        or book.amount is None
        or bank.txn_date is None
        or book.txn_date is None
    ):
        return False
    magnitude = max(abs(bank.amount), abs(book.amount))
    tolerance = min(
        NEAR_AMOUNT_ABSOLUTE_CAP,
        max(NEAR_AMOUNT_ABSOLUTE_FLOOR, magnitude * NEAR_AMOUNT_RELATIVE_RATE),
    )
    return (
        abs(bank.amount - book.amount) <= tolerance
        and abs((bank.txn_date - book.txn_date).days) <= NEAR_AMOUNT_DATE_DAYS
    )


def _chunks[T](rows: tuple[T, ...]) -> tuple[tuple[T, ...], ...]:
    return tuple(
        rows[offset : offset + MAX_CANDIDATES_PER_SIDE]
        for offset in range(0, len(rows), MAX_CANDIDATES_PER_SIDE)
    )


def _candidate_coordinates(
    bank_chunks: tuple[tuple[UnmatchedBank, ...], ...],
    book_chunks: tuple[tuple[UnmatchedBook, ...], ...],
) -> tuple[tuple[int, int], ...]:
    """Identify bounded tiles containing at least one deterministic plausible edge.

    A pair is plausible when its non-null signed amounts are exactly equal, its
    normalized memos share a distinctive token, OR its dated amounts differ by
    at most ``min($5, max($0.50, 1%))`` within seven days.  These are bounded,
    provider-independent signals.  Date is not a hard gate for exact-amount or
    distinctive-memo evidence: the residual lane must retain delayed checks and
    other real transactions outside the deterministic/fuzzy matcher's windows.
    """

    bank_amount_blocks: dict[Decimal, set[int]] = {}
    book_amount_blocks: dict[Decimal, set[int]] = {}
    bank_token_blocks: dict[str, set[int]] = {}
    book_token_blocks: dict[str, set[int]] = {}
    for block_index, rows in enumerate(bank_chunks):
        for row in rows:
            if row.amount is not None:
                bank_amount_blocks.setdefault(row.amount, set()).add(block_index)
            for token in _memo_tokens(row.description):
                bank_token_blocks.setdefault(token, set()).add(block_index)
    for block_index, rows in enumerate(book_chunks):
        for row in rows:
            if row.amount is not None:
                book_amount_blocks.setdefault(row.amount, set()).add(block_index)
            for token in _memo_tokens(row.description):
                book_token_blocks.setdefault(token, set()).add(block_index)

    coordinates: set[tuple[int, int]] = set()
    for amount in bank_amount_blocks.keys() & book_amount_blocks.keys():
        coordinates.update(
            (bank_index, book_index)
            for bank_index in bank_amount_blocks[amount]
            for book_index in book_amount_blocks[amount]
        )
    for token in bank_token_blocks.keys() & book_token_blocks.keys():
        coordinates.update(
            (bank_index, book_index)
            for bank_index in bank_token_blocks[token]
            for book_index in book_token_blocks[token]
        )
    for bank_index, bank_rows in enumerate(bank_chunks):
        for book_index, book_rows in enumerate(book_chunks):
            if (bank_index, book_index) in coordinates:
                continue
            if any(
                _near_amount_and_date(bank, book)
                for bank in bank_rows
                for book in book_rows
            ):
                coordinates.add((bank_index, book_index))
    return tuple(sorted(coordinates))


def _split_page_to_budget(
    bank_rows: tuple[UnmatchedBank, ...],
    book_rows: tuple[UnmatchedBook, ...],
) -> tuple[_CandidatePage, ...]:
    if not bank_rows or not book_rows:
        return (_CandidatePage(bank_rows, book_rows, None, 0),)
    prompt = _build_user_prompt(bank_rows, book_rows)
    estimate = _estimated_input_tokens(prompt)
    if estimate <= MAX_INPUT_TOKENS:
        return (_CandidatePage(bank_rows, book_rows, prompt, estimate),)
    if len(bank_rows) == 1 and len(book_rows) == 1:
        raise SuggestionInputBudgetError(
            "A minimum suggestion batch exceeds the input-token budget."
        )
    if len(bank_rows) >= len(book_rows) and len(bank_rows) > 1:
        midpoint = len(bank_rows) // 2
        return _split_page_to_budget(
            bank_rows[:midpoint], book_rows
        ) + _split_page_to_budget(bank_rows[midpoint:], book_rows)
    midpoint = len(book_rows) // 2
    return _split_page_to_budget(
        bank_rows, book_rows[:midpoint]
    ) + _split_page_to_budget(bank_rows, book_rows[midpoint:])


def _candidate_pages(
    bank_rows: tuple[UnmatchedBank, ...],
    book_rows: tuple[UnmatchedBook, ...],
) -> tuple[_CandidatePage, ...]:
    bank_chunks = _chunks(bank_rows)
    book_chunks = _chunks(book_rows)
    if not bank_chunks and not book_chunks:
        return ()
    if not book_chunks:
        return tuple(_CandidatePage(rows, (), None, 0) for rows in bank_chunks)
    if not bank_chunks:
        return tuple(_CandidatePage((), rows, None, 0) for rows in book_chunks)

    pages: list[_CandidatePage] = []
    for bank_index, book_index in _candidate_coordinates(bank_chunks, book_chunks):
        pages.extend(
            _split_page_to_budget(bank_chunks[bank_index], book_chunks[book_index])
        )
    return tuple(pages)


def _eligibility_hash(pages: tuple[_CandidatePage, ...]) -> str:
    material = {
        "plausibility_version": _PLAUSIBILITY_VERSION,
        "pages": [
            {
                "bank_ids": [row.bank_item_id for row in page.bank_rows],
                "book_ids": [row.book_txn_id for row in page.book_rows],
            }
            for page in pages
        ],
    }
    canonical = json.dumps(material, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _traversal_id(
    scope: SuggestionScope,
    *,
    bank_hash: str,
    book_hash: str,
    eligibility_hash: str,
    bank_count: int,
    book_count: int,
) -> str:
    material = json.dumps(
        {
            "v": _CURSOR_VERSION,
            "o": scope.organization_id,
            "c": scope.organization_company_id,
            "r": scope.realm_id,
            "s": scope.session_id,
            "i": scope.statement_import_id,
            "l": scope.lineage_status,
            "bh": bank_hash,
            "kh": book_hash,
            "bc": bank_count,
            "kc": book_count,
            "eh": eligibility_hash,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def _page_fingerprint(
    scope: SuggestionScope, state: _CursorState, page: _CandidatePage
) -> str:
    material = {
        "scope": _cursor_payload(scope, replace(state, attempt=0)),
        "bank_ids": [row.bank_item_id for row in page.bank_rows],
        "book_ids": [row.book_txn_id for row in page.book_rows],
    }
    canonical = json.dumps(material, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _attempt_fingerprint(page_fingerprint: str, attempt: int) -> str:
    return hashlib.sha256(
        f"{page_fingerprint}:attempt:{attempt}".encode("ascii")
    ).hexdigest()


def _step(attempt_fingerprint: str, *, attempt: int) -> str:
    return f"bank-recon-suggest:v2:{attempt_fingerprint[:32]}:a{attempt}"


def _validate_state(
    state: _CursorState,
    bank_rows: tuple[UnmatchedBank, ...],
    book_rows: tuple[UnmatchedBook, ...],
    bank_hash: str,
    book_hash: str,
    eligibility_hash: str,
    page_count: int,
    traversal_id: str,
) -> None:
    if (
        state.bank_hash != bank_hash
        or state.book_hash != book_hash
        or state.bank_count != len(bank_rows)
        or state.book_count != len(book_rows)
        or state.eligibility_hash != eligibility_hash
        or state.page_count != page_count
        or state.traversal_id != traversal_id
        or not 0 <= state.page_index < max(page_count, 1)
    ):
        raise SuggestionCursorError(
            "Suggestion cursor is stale for this candidate set."
        )


def _resolve_scope(db: Session, scope: SuggestionScope) -> SuggestionScope:
    """Resolve and verify the complete tenant/company/realm identity."""

    query = db.query(OrganizationCompany).filter(
        OrganizationCompany.realm_id == scope.realm_id
    )
    if scope.organization_id is not None:
        query = query.filter(
            OrganizationCompany.organization_id == scope.organization_id
        )
    if scope.organization_company_id is not None:
        query = query.filter(
            OrganizationCompany.id == scope.organization_company_id
        )
    company = query.one_or_none()
    if company is None:
        raise SuggestionRetryRefused(
            "The reconciliation suggestion tenant scope is unavailable."
        )
    return replace(
        scope,
        organization_id=company.organization_id,
        organization_company_id=company.id,
    )


def _usage_events_for_started_attempt(
    db: Session,
    *,
    scope: SuggestionScope,
    provider: str,
    model: str,
    step: str,
) -> list[LLMUsageEvent]:
    """Find only telemetry that exactly matches one started attempt."""

    return (
        db.query(LLMUsageEvent)
        .filter(
            LLMUsageEvent.organization_id == scope.organization_id,
            LLMUsageEvent.organization_company_id
            == scope.organization_company_id,
            LLMUsageEvent.qbo_realm_id == scope.realm_id,
            LLMUsageEvent.provider == provider,
            LLMUsageEvent.model == model,
            LLMUsageEvent.task_type == _TASK_TYPE,
            LLMUsageEvent.step == step,
        )
        .order_by(LLMUsageEvent.id)
        .all()
    )


def _exact_usage_event(
    db: Session,
    *,
    usage_event_id: int,
    scope: SuggestionScope,
    provider: str,
    model: str,
    step: str,
) -> LLMUsageEvent | None:
    """Resolve an exact result-bearing event; every identity axis must match."""

    return (
        db.query(LLMUsageEvent)
        .filter(
            LLMUsageEvent.id == usage_event_id,
            LLMUsageEvent.organization_id == scope.organization_id,
            LLMUsageEvent.organization_company_id
            == scope.organization_company_id,
            LLMUsageEvent.qbo_realm_id == scope.realm_id,
            LLMUsageEvent.provider == provider,
            LLMUsageEvent.model == model,
            LLMUsageEvent.task_type == _TASK_TYPE,
            LLMUsageEvent.step == step,
        )
        .one_or_none()
    )


def _lock_exact_session(db: Session, scope: SuggestionScope) -> ReconciliationSession:
    if scope.organization_id is None or scope.organization_company_id is None:
        raise SuggestionRetryRefused(
            "The reconciliation suggestion tenant scope is incomplete."
        )
    company = (
        db.query(OrganizationCompany.id)
        .filter(
            OrganizationCompany.id == scope.organization_company_id,
            OrganizationCompany.organization_id == scope.organization_id,
            OrganizationCompany.realm_id == scope.realm_id,
        )
        .one_or_none()
    )
    if company is None:
        raise SuggestionRetryRefused(
            "The reconciliation suggestion tenant scope changed."
        )
    query = db.query(ReconciliationSession).filter(
        ReconciliationSession.id == scope.session_id,
        ReconciliationSession.realm_id == scope.realm_id,
        ReconciliationSession.lineage_status == scope.lineage_status,
    )
    if scope.statement_import_id is None:
        query = query.filter(ReconciliationSession.statement_import_id.is_(None))
    else:
        query = query.filter(
            ReconciliationSession.statement_import_id == scope.statement_import_id
        )
    row = query.with_for_update().one_or_none()
    if row is None:
        raise SuggestionRetryRefused("The reconciliation session scope changed.")
    return row


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _suggestion_payload(suggestion: Suggestion) -> dict[str, object]:
    return {
        "bank_item_id": suggestion.bank_item_id,
        "book_txn_id": suggestion.book_txn_id,
        "amount": _amount_text(suggestion.amount),
        "confidence": suggestion.confidence,
        "reason": suggestion.reason,
    }


def _result_payload(result: SuggestResult) -> dict[str, object]:
    return {
        "suggestions": [_suggestion_payload(item) for item in result.suggestions],
        "truncated": result.truncated,
        "page_number": result.page_number,
        "planned_pages": result.planned_pages,
        "next_cursor": result.next_cursor,
        "has_more": result.has_more,
        "retry_cursor": result.retry_cursor,
        "failure_code": result.failure_code,
    }


def _stored_result(row: BankReconciliationSuggestionAttempt) -> SuggestResult:
    if row.status not in _TERMINAL_STATUSES:
        raise SuggestionRetryRefused("The suggestion attempt ledger is inconsistent.")
    payload = row.result_json
    if not isinstance(payload, dict) or set(payload) != {
        "suggestions",
        "truncated",
        "page_number",
        "planned_pages",
        "next_cursor",
        "has_more",
        "retry_cursor",
        "failure_code",
    }:
        raise SuggestionRetryRefused("The suggestion attempt ledger is inconsistent.")
    raw_suggestions = payload.get("suggestions")
    if not isinstance(raw_suggestions, list):
        raise SuggestionRetryRefused("The suggestion attempt ledger is inconsistent.")
    suggestions: list[Suggestion] = []
    for item in raw_suggestions:
        if not isinstance(item, dict) or set(item) != {
            "bank_item_id",
            "book_txn_id",
            "amount",
            "confidence",
            "reason",
        }:
            raise SuggestionRetryRefused(
                "The suggestion attempt ledger is inconsistent."
            )
        bank_id = item.get("bank_item_id")
        book_id = item.get("book_txn_id")
        confidence = item.get("confidence")
        amount_text = item.get("amount")
        reason = item.get("reason")
        if (
            isinstance(bank_id, bool)
            or not isinstance(bank_id, int)
            or isinstance(book_id, bool)
            or not isinstance(book_id, int)
            or isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or (amount_text is not None and not isinstance(amount_text, str))
            or not isinstance(reason, str)
        ):
            raise SuggestionRetryRefused(
                "The suggestion attempt ledger is inconsistent."
            )
        try:
            amount = Decimal(amount_text) if amount_text is not None else None
        except InvalidOperation as exc:
            raise SuggestionRetryRefused(
                "The suggestion attempt ledger is inconsistent."
            ) from exc
        suggestions.append(
            Suggestion(
                bank_item_id=bank_id,
                book_txn_id=book_id,
                amount=amount,
                confidence=float(confidence),
                reason=reason,
            )
        )
    page_number = payload.get("page_number")
    planned_pages = payload.get("planned_pages")
    if (
        not isinstance(payload.get("truncated"), bool)
        or isinstance(page_number, bool)
        or not isinstance(page_number, int)
        or isinstance(planned_pages, bool)
        or not isinstance(planned_pages, int)
        or not isinstance(payload.get("has_more"), bool)
        or (
            payload.get("next_cursor") is not None
            and not isinstance(payload.get("next_cursor"), str)
        )
        or (
            payload.get("retry_cursor") is not None
            and not isinstance(payload.get("retry_cursor"), str)
        )
        or (
            payload.get("failure_code") is not None
            and not isinstance(payload.get("failure_code"), str)
        )
    ):
        raise SuggestionRetryRefused("The suggestion attempt ledger is inconsistent.")
    return SuggestResult(
        suggestions=suggestions,
        truncated=payload["truncated"],
        page_number=page_number,
        planned_pages=planned_pages,
        next_cursor=payload["next_cursor"],
        has_more=payload["has_more"],
        retry_cursor=payload["retry_cursor"],
        failure_code=payload["failure_code"],
    )


def _attempt_query(
    db: Session, prepared: PreparedSuggestionBatch
):
    scope = prepared.scope
    return db.query(BankReconciliationSuggestionAttempt).filter(
        BankReconciliationSuggestionAttempt.organization_id
        == scope.organization_id,
        BankReconciliationSuggestionAttempt.organization_company_id
        == scope.organization_company_id,
        BankReconciliationSuggestionAttempt.qbo_realm_id == scope.realm_id,
        BankReconciliationSuggestionAttempt.attempt_fingerprint
        == prepared.attempt_fingerprint,
    )


def _assert_attempt_identity(
    row: BankReconciliationSuggestionAttempt,
    prepared: PreparedSuggestionBatch,
) -> None:
    scope = prepared.scope
    expected = (
        scope.organization_id,
        scope.organization_company_id,
        scope.realm_id,
        scope.session_id,
        scope.statement_import_id,
        scope.lineage_status,
        prepared.state.traversal_id,
        prepared.state.page_index,
        prepared.state.page_count,
        prepared.page_fingerprint,
        prepared.state.attempt,
        prepared.attempt_fingerprint,
        _TASK_TYPE,
        prepared.current_step,
    )
    observed = (
        row.organization_id,
        row.organization_company_id,
        row.qbo_realm_id,
        row.reconciliation_session_id,
        row.statement_import_id,
        row.lineage_status,
        row.traversal_id,
        row.page_index,
        row.page_count,
        row.page_fingerprint,
        row.attempt_ordinal,
        row.attempt_fingerprint,
        row.task_type,
        row.step,
    )
    if observed != expected:
        raise SuggestionRetryRefused("The suggestion attempt ledger is inconsistent.")


def _load_attempt(
    db: Session,
    prepared: PreparedSuggestionBatch,
    *,
    for_update: bool,
) -> BankReconciliationSuggestionAttempt | None:
    query = _attempt_query(db, prepared)
    if for_update:
        query = query.with_for_update()
    row = query.one_or_none()
    if row is not None:
        _assert_attempt_identity(row, prepared)
    return row


def _failure_result(
    prepared: PreparedSuggestionBatch,
    failure_code: str,
    *,
    retry: bool = False,
) -> SuggestResult:
    return SuggestResult(
        suggestions=[],
        truncated=prepared.has_more,
        page_number=prepared.state.page_index + 1,
        planned_pages=prepared.state.page_count,
        next_cursor=prepared.next_cursor,
        has_more=prepared.has_more,
        retry_cursor=_retry_cursor(prepared) if retry else None,
        failure_code=failure_code,
    )


def _persist_terminal_result(
    db: Session,
    prepared: PreparedSuggestionBatch,
    *,
    attempt: BankReconciliationSuggestionAttempt,
    result: SuggestResult,
    status: str,
    verified_usage_event_id: int | None,
) -> SuggestResult:
    if status not in _TERMINAL_STATUSES:
        raise SuggestionRetryRefused("Invalid suggestion terminal status.")
    if attempt.status in _TERMINAL_STATUSES:
        return _stored_result(attempt)
    if attempt.status != "started":
        raise SuggestionRetryRefused("The suggestion attempt was not started.")
    if attempt.usage_event_id is None:
        raise SuggestionRetryRefused(
            "The suggestion attempt has no pre-call usage authorization."
        )
    if (
        verified_usage_event_id is not None
        and verified_usage_event_id != attempt.usage_event_id
    ):
        raise SuggestionRetryRefused(
            "The suggestion usage evidence does not match its authorization."
        )
    attempt.status = status
    attempt.failure_code = None if status == "succeeded" else status
    attempt.result_json = _result_payload(result)
    attempt.terminal_at = _utcnow()
    db.commit()
    return result


def _retry_is_authorized(db: Session, prepared: PreparedSuggestionBatch) -> bool:
    first_fingerprint = _attempt_fingerprint(prepared.page_fingerprint, 0)
    first = replace(
        prepared,
        state=replace(prepared.state, attempt=0),
        attempt_fingerprint=first_fingerprint,
        current_step=_step(first_fingerprint, attempt=0),
    )
    attempt = _load_attempt(db, first, for_update=False)
    if (
        attempt is None
        or attempt.status != "provider_error"
        or attempt.failure_code != "provider_error"
        or attempt.usage_event_id is None
        or attempt.provider is None
        or attempt.model is None
    ):
        return False
    events = _usage_events_for_started_attempt(
        db,
        scope=prepared.scope,
        provider=attempt.provider,
        model=attempt.model,
        step=prepared.first_attempt_step,
    )
    if len(events) != 1:
        return False
    event = events[0]
    return bool(
        event.id == attempt.usage_event_id
        and event.status == "error"
        and _token_count(event.input_tokens) is not None
        and _token_count(event.output_tokens) is not None
    )


def _reconcile_stale_started_attempt(
    db: Session,
    prepared: PreparedSuggestionBatch,
    attempt: BankReconciliationSuggestionAttempt,
) -> SuggestResult:
    if attempt.started_at is None or attempt.provider is None or attempt.model is None:
        raise SuggestionRetryRefused("The suggestion attempt ledger is inconsistent.")
    if _utcnow() - _aware_utc(attempt.started_at) < _STARTED_STALE_AFTER:
        raise SuggestionRetryRefused(
            "This suggestion attempt is already in progress or indeterminate."
        )
    events = _usage_events_for_started_attempt(
        db,
        scope=prepared.scope,
        provider=attempt.provider,
        model=attempt.model,
        step=attempt.step,
    )
    usage_event_id: int | None = attempt.usage_event_id
    retry = False
    if not events:
        status = "usage_unmetered"
    elif len(events) > 1:
        status = "usage_ambiguous"
    else:
        event = events[0]
        if event.id != attempt.usage_event_id:
            status = "usage_mismatch"
        elif event.status == "error" and (
            _token_count(event.input_tokens) is not None
            and _token_count(event.output_tokens) is not None
        ):
            status = "provider_error"
            retry = attempt.attempt_ordinal == 0
        elif event.status == "ok":
            status = "result_unavailable"
        elif event.status == "started":
            status = "provider_timeout"
        else:
            status = "usage_mismatch"
    result = _failure_result(prepared, status, retry=retry)
    return _persist_terminal_result(
        db,
        prepared,
        attempt=attempt,
        result=result,
        status=status,
        verified_usage_event_id=usage_event_id,
    )


def _claim_attempt(
    db: Session,
    prepared: PreparedSuggestionBatch,
    *,
    user_id: int | None,
) -> PreparedSuggestionBatch:
    _lock_exact_session(db, prepared.scope)
    attempt = _load_attempt(db, prepared, for_update=True)
    if attempt is not None:
        with_id = replace(prepared, spend_claim_id=attempt.id)
        if attempt.status in _TERMINAL_STATUSES:
            return replace(prepared, cached_result=_stored_result(attempt))
        if attempt.status == "claimed":
            return with_id
        if attempt.status == "started":
            recovered = _reconcile_stale_started_attempt(db, with_id, attempt)
            return replace(prepared, cached_result=recovered)
        raise SuggestionRetryRefused("The suggestion attempt ledger is inconsistent.")
    if prepared.state.attempt == 1 and not _retry_is_authorized(db, prepared):
        raise SuggestionRetryRefused(
            "Retry requires one durable first-attempt provider failure."
        )
    claim = BankReconciliationSuggestionAttempt(
        organization_id=prepared.scope.organization_id,
        organization_company_id=prepared.scope.organization_company_id,
        qbo_realm_id=prepared.scope.realm_id,
        reconciliation_session_id=prepared.scope.session_id,
        statement_import_id=prepared.scope.statement_import_id,
        lineage_status=prepared.scope.lineage_status,
        traversal_id=prepared.state.traversal_id,
        page_index=prepared.state.page_index,
        page_count=prepared.state.page_count,
        page_fingerprint=prepared.page_fingerprint,
        attempt_ordinal=prepared.state.attempt,
        attempt_fingerprint=prepared.attempt_fingerprint,
        task_type=_TASK_TYPE,
        step=prepared.current_step,
        status="claimed",
        claimed_at=_utcnow(),
    )
    db.add(claim)
    db.flush()
    return replace(prepared, spend_claim_id=claim.id)


def prepare_suggestion_batch(
    db: Session,
    *,
    scope: SuggestionScope,
    bank_rows: Sequence[UnmatchedBank],
    book_rows: Sequence[UnmatchedBook],
    cursor: str | None = None,
    user_id: int | None = None,
) -> PreparedSuggestionBatch:
    """Validate one page and flush its deterministic write-ahead spend claim.

    A non-null ``spend_claim_id`` means the caller MUST commit ``db`` before
    passing the batch to :func:`execute_suggestion_batch`.
    """

    scope = _resolve_scope(db, scope)
    ordered_bank = _ordered_bank(bank_rows)
    ordered_book = _ordered_book(book_rows)
    bank_hash = _rows_hash(_fmt_bank(ordered_bank))
    book_hash = _rows_hash(_fmt_book(ordered_book))
    pages = _candidate_pages(ordered_bank, ordered_book)
    eligibility_hash = _eligibility_hash(pages)
    traversal_id = _traversal_id(
        scope,
        bank_hash=bank_hash,
        book_hash=book_hash,
        eligibility_hash=eligibility_hash,
        bank_count=len(ordered_bank),
        book_count=len(ordered_book),
    )
    state = (
        _decode_cursor(cursor, scope)
        if cursor
        else _CursorState(
            traversal_id=traversal_id,
            page_index=0,
            attempt=0,
            bank_hash=bank_hash,
            book_hash=book_hash,
            bank_count=len(ordered_bank),
            book_count=len(ordered_book),
            eligibility_hash=eligibility_hash,
            page_count=len(pages),
        )
    )
    _validate_state(
        state,
        ordered_bank,
        ordered_book,
        bank_hash,
        book_hash,
        eligibility_hash,
        len(pages),
        traversal_id,
    )
    page = pages[state.page_index] if pages else _CandidatePage((), (), None, 0)
    has_more = state.page_index + 1 < len(pages)
    next_cursor = None
    if has_more:
        next_cursor = _encode_cursor(
            scope,
            replace(
                state,
                page_index=state.page_index + 1,
                attempt=0,
            ),
        )
    page_fingerprint = _page_fingerprint(scope, state, page)
    attempt_fingerprint = _attempt_fingerprint(page_fingerprint, state.attempt)
    prepared = PreparedSuggestionBatch(
        scope=scope,
        state=state,
        bank_rows=page.bank_rows,
        book_rows=page.book_rows,
        user_prompt=page.user_prompt,
        estimated_input_tokens=page.estimated_input_tokens,
        next_cursor=next_cursor,
        has_more=has_more,
        current_step=_step(attempt_fingerprint, attempt=state.attempt),
        first_attempt_step=_step(_attempt_fingerprint(page_fingerprint, 0), attempt=0),
        page_fingerprint=page_fingerprint,
        attempt_fingerprint=attempt_fingerprint,
        user_id=user_id,
    )
    if not page.bank_rows or not page.book_rows:
        return prepared
    return _claim_attempt(db, prepared, user_id=user_id)


def _resolve_provider_route(llm_service: _JsonLLM) -> _ResolvedSuggestionRoute:
    """Resolve budget-aware production routing before spend is authorized."""

    try:
        resolver = getattr(llm_service, "resolve_effective_route", None)
        if callable(resolver):
            resolved = resolver(model=None)
            service = resolved.service
            config = resolved.config
            model = resolved.model.strip()
        else:
            service = llm_service
            config = llm_service.config
            model = config.model.strip()
        provider = config.provider.strip()
        lane = config.lane.strip()
    except (AttributeError, TypeError) as exc:
        raise SuggestionRetryRefused(
            "The suggestion provider identity is unavailable."
        ) from exc
    if not provider or not model or not lane:
        raise SuggestionRetryRefused(
            "The suggestion provider identity is unavailable."
        )
    return _ResolvedSuggestionRoute(
        service=service,
        provider=provider,
        model=model,
        lane=lane,
    )


def _consume_spend_claim(
    db: Session,
    prepared: PreparedSuggestionBatch,
    *,
    provider: str,
    model: str,
    lane: str,
) -> SuggestResult | _SpendAuthorization:
    """Atomically consume a committed claim before the provider boundary."""

    if prepared.spend_claim_id is None:
        raise SuggestionRetryClaimNotCommitted(
            "A durable spend claim is required before provider execution."
        )
    # The prepare transaction must be over before this function begins.  This
    # makes the claim survive a process/provider failure after spend starts.
    if db.in_transaction():
        raise SuggestionRetryClaimNotCommitted(
            "Commit the spend claim before executing the provider call."
        )
    attempt = (
        db.query(BankReconciliationSuggestionAttempt)
        .filter(
            BankReconciliationSuggestionAttempt.id == prepared.spend_claim_id,
            BankReconciliationSuggestionAttempt.organization_id
            == prepared.scope.organization_id,
            BankReconciliationSuggestionAttempt.organization_company_id
            == prepared.scope.organization_company_id,
            BankReconciliationSuggestionAttempt.qbo_realm_id
            == prepared.scope.realm_id,
            BankReconciliationSuggestionAttempt.attempt_fingerprint
            == prepared.attempt_fingerprint,
        )
        .with_for_update()
        .one_or_none()
    )
    if attempt is None:
        raise SuggestionRetryClaimNotCommitted(
            "The spend claim was not durably persisted."
        )
    _assert_attempt_identity(attempt, prepared)
    if attempt.status in _TERMINAL_STATUSES:
        result = _stored_result(attempt)
        db.commit()
        return result
    if attempt.status == "started":
        return _reconcile_stale_started_attempt(db, prepared, attempt)
    if attempt.status != "claimed":
        raise SuggestionRetryRefused("The suggestion attempt ledger is inconsistent.")
    placeholder = LLMUsageEvent(
        organization_id=prepared.scope.organization_id,
        organization_company_id=prepared.scope.organization_company_id,
        qbo_realm_id=prepared.scope.realm_id,
        user_id=prepared.user_id,
        module="llm_lane",
        route=lane,
        task_type=_TASK_TYPE,
        step=prepared.current_step,
        provider=provider,
        model=model,
        status="started",
        tool_call_count=0,
        tool_error_count=0,
    )
    db.add(placeholder)
    # Flush only the new event.  Flushing the session here would also emit the
    # still-``claimed`` attempt's provider/model changes before its required
    # usage FK is assigned, transiently violating the lifecycle constraint.
    db.flush([placeholder])
    usage_event_id = placeholder.id
    attempt.provider = provider
    attempt.model = model
    attempt.usage_event_id = usage_event_id
    attempt.status = "started"
    attempt.started_at = _utcnow()
    # This commit is the irreversible spend authorization. The exact full-scope
    # usage row and attempt start become durable together; no SDK boundary is
    # crossed until both succeed, and no later execution can consume it again.
    db.commit()
    return _SpendAuthorization(usage_event_id=usage_event_id)


def _lock_started_attempt(
    db: Session,
    prepared: PreparedSuggestionBatch,
    *,
    provider: str,
    model: str,
) -> BankReconciliationSuggestionAttempt:
    if prepared.spend_claim_id is None:
        raise SuggestionRetryClaimNotCommitted(
            "A durable spend claim is required before provider execution."
        )
    attempt = (
        db.query(BankReconciliationSuggestionAttempt)
        .filter(
            BankReconciliationSuggestionAttempt.id == prepared.spend_claim_id,
            BankReconciliationSuggestionAttempt.organization_id
            == prepared.scope.organization_id,
            BankReconciliationSuggestionAttempt.organization_company_id
            == prepared.scope.organization_company_id,
            BankReconciliationSuggestionAttempt.qbo_realm_id
            == prepared.scope.realm_id,
        )
        .with_for_update()
        .one_or_none()
    )
    if attempt is None:
        raise SuggestionRetryClaimNotCommitted(
            "The spend claim was not durably persisted."
        )
    _assert_attempt_identity(attempt, prepared)
    if attempt.status in _TERMINAL_STATUSES:
        return attempt
    if (
        attempt.status != "started"
        or attempt.provider != provider
        or attempt.model != model
        or attempt.usage_event_id is None
    ):
        raise SuggestionRetryRefused("The suggestion attempt ledger is inconsistent.")
    return attempt


def _token_count(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _classify_result_evidence(
    db: Session,
    prepared: PreparedSuggestionBatch,
    *,
    call_result: LLMJSONCallResult | None,
    provider: str,
    model: str,
    expected_usage_event_id: int,
) -> tuple[str, int | None, dict | None]:
    """Validate exact telemetry and return terminal status/link/payload."""

    if call_result is None or call_result.usage_event_id is None:
        return "usage_unmetered", None, None
    if (
        isinstance(call_result.usage_event_id, bool)
        or not isinstance(call_result.usage_event_id, int)
        or call_result.usage_event_id != expected_usage_event_id
        or call_result.provider != provider
        or call_result.model != model
    ):
        return "usage_mismatch", None, None
    events = _usage_events_for_started_attempt(
        db,
        scope=prepared.scope,
        provider=provider,
        model=model,
        step=prepared.current_step,
    )
    if not events:
        return "usage_unmetered", None, None
    if len(events) > 1:
        return "usage_ambiguous", expected_usage_event_id, None
    event = events[0]
    if event.id != expected_usage_event_id:
        return "usage_mismatch", None, None
    usage_event_id = event.id
    raw = call_result.payload
    valid_shape = isinstance(raw, dict) and isinstance(raw.get("suggestions"), list)
    if call_result.truncated:
        return "response_truncated", usage_event_id, None
    usage = call_result.token_usage
    if not isinstance(usage, dict):
        return "usage_mismatch", usage_event_id, None
    result_input = _token_count(usage.get("input_tokens"))
    result_output = _token_count(usage.get("output_tokens"))
    event_input = _token_count(event.input_tokens)
    event_output = _token_count(event.output_tokens)
    if (
        result_input is None
        or result_output is None
        or event_input is None
        or event_output is None
        or result_input != event_input
        or result_output != event_output
        or result_input > MAX_INPUT_TOKENS
        or result_output > MAX_OUTPUT_TOKENS
    ):
        return "usage_mismatch", usage_event_id, None
    if event.status == "error" and raw is None:
        return "provider_error", usage_event_id, None
    if event.status != "ok":
        return "usage_mismatch", usage_event_id, None
    if not valid_shape:
        return "invalid_response", usage_event_id, None
    return "succeeded", usage_event_id, raw


def _field(item: dict, key: str) -> object:
    return item[key] if key in item else None


def _coerce_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _coerce_confidence(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return 0.0
    try:
        confidence = float(value)
    except ValueError:
        return 0.0
    return min(1.0, max(0.0, confidence))


def _validate_and_build(
    raw: dict,
    bank_rows: Sequence[UnmatchedBank],
    book_rows: Sequence[UnmatchedBook],
) -> list[Suggestion]:
    bank_by_id = {row.bank_item_id: row for row in bank_rows}
    book_ids = {row.book_txn_id for row in book_rows}
    items = raw.get("suggestions")
    if not isinstance(items, list):
        return []
    suggestions: list[Suggestion] = []
    seen_pairs: set[tuple[int, int]] = set()
    used_bank_ids: set[int] = set()
    used_book_ids: set[int] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        bank_id = _coerce_int(_field(item, "bank_item_id"))
        book_id = _coerce_int(_field(item, "book_txn_id"))
        if bank_id not in bank_by_id or book_id not in book_ids:
            continue
        pair = (bank_id, book_id)
        if pair in seen_pairs or bank_id in used_bank_ids or book_id in used_book_ids:
            continue
        seen_pairs.add(pair)
        used_bank_ids.add(bank_id)
        used_book_ids.add(book_id)
        suggestions.append(
            Suggestion(
                bank_item_id=bank_id,
                book_txn_id=book_id,
                amount=bank_by_id[bank_id].amount,
                confidence=_coerce_confidence(_field(item, "confidence")),
                reason=str(_field(item, "reason") or "").strip()[:500],
            )
        )
    return suggestions


def _retry_cursor(prepared: PreparedSuggestionBatch) -> str:
    return _encode_cursor(
        prepared.scope,
        replace(prepared.state, attempt=1),
    )


def execute_suggestion_batch(
    db: Session,
    prepared: PreparedSuggestionBatch,
    *,
    llm_service: _JsonLLM | None = None,
) -> SuggestResult:
    """Perform at most one provider call for a prepared, proposal-only batch."""

    if prepared.cached_result is not None:
        return prepared.cached_result
    base = {
        "truncated": prepared.has_more,
        "page_number": (
            prepared.state.page_index + 1 if prepared.state.page_count else 0
        ),
        "planned_pages": prepared.state.page_count,
        "next_cursor": prepared.next_cursor,
        "has_more": prepared.has_more,
    }
    if not prepared.bank_rows or not prepared.book_rows:
        return SuggestResult(suggestions=[], **base)
    if (
        prepared.user_prompt is None
        or prepared.estimated_input_tokens > MAX_INPUT_TOKENS
    ):
        raise SuggestionInputBudgetError(
            "Suggestion batch exceeds the input-token budget."
        )
    if llm_service is None:
        from ..llm import get_llm_service

        llm_service = get_llm_service(_LLM_LANE)
    route = _resolve_provider_route(llm_service)
    authorization = _consume_spend_claim(
        db,
        prepared,
        provider=route.provider,
        model=route.model,
        lane=route.lane,
    )
    if isinstance(authorization, SuggestResult):
        return authorization

    call_result: LLMJSONCallResult | None = None
    try:
        with bind_tenant_tag(
            organization_id=prepared.scope.organization_id,
            organization_company_id=prepared.scope.organization_company_id,
            qbo_realm_id=prepared.scope.realm_id,
            user_id=prepared.user_id,
        ):
            with bind_llm_task_context(
                _TASK_TYPE,
                step=prepared.current_step,
                realm_id=prepared.scope.realm_id,
            ):
                call_result = route.service.predict_json_result(
                    _SYSTEM_PROMPT,
                    prepared.user_prompt,
                    temperature=0.0,
                    model=route.model,
                    allow_fallback=False,
                    task_type=_TASK_TYPE,
                    timeout_override=PROVIDER_TIMEOUT_SECONDS,
                    max_retries_override=SDK_RETRIES,
                    max_output_tokens_override=MAX_OUTPUT_TOKENS,
                    preallocated_usage_event_id=authorization.usage_event_id,
                )
    except Exception:
        # The durable ``started`` state already prevents a second spend.  A
        # missing result-bearing return cannot be guessed from loose telemetry;
        # it becomes a terminal unmetered failure below.
        call_result = None

    status, usage_event_id, raw = _classify_result_evidence(
        db,
        prepared,
        call_result=call_result,
        provider=route.provider,
        model=route.model,
        expected_usage_event_id=authorization.usage_event_id,
    )
    attempt = _lock_started_attempt(
        db,
        prepared,
        provider=route.provider,
        model=route.model,
    )
    if attempt.status in _TERMINAL_STATUSES:
        result = _stored_result(attempt)
        db.commit()
        return result
    if status == "succeeded" and raw is not None:
        result = SuggestResult(
            suggestions=_validate_and_build(
                raw, prepared.bank_rows, prepared.book_rows
            ),
            **base,
        )
        return _persist_terminal_result(
            db,
            prepared,
            attempt=attempt,
            result=result,
            status="succeeded",
            verified_usage_event_id=usage_event_id,
        )
    retry_available = status == "provider_error" and prepared.state.attempt == 0
    result = _failure_result(
        prepared,
        status,
        retry=retry_available,
    )
    return _persist_terminal_result(
        db,
        prepared,
        attempt=attempt,
        result=result,
        status=status,
        verified_usage_event_id=usage_event_id,
    )


__all__ = [
    "MAX_CANDIDATES_PER_SIDE",
    "MAX_INPUT_TOKENS",
    "MAX_OUTPUT_TOKENS",
    "NEAR_AMOUNT_ABSOLUTE_CAP",
    "NEAR_AMOUNT_ABSOLUTE_FLOOR",
    "NEAR_AMOUNT_DATE_DAYS",
    "NEAR_AMOUNT_RELATIVE_RATE",
    "PROVIDER_TIMEOUT_SECONDS",
    "SDK_RETRIES",
    "PreparedSuggestionBatch",
    "Suggestion",
    "SuggestionCursorError",
    "SuggestionInputBudgetError",
    "SuggestionRetryClaimNotCommitted",
    "SuggestionRetryRefused",
    "SuggestionScope",
    "SuggestResult",
    "execute_suggestion_batch",
    "prepare_suggestion_batch",
]

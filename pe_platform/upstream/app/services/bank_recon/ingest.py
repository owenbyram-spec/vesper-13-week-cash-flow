"""
app/services/bank_recon/ingest.py — bank-statement import+parse service (C1-a).

The FAT service behind ``POST /bank-reconciliation/import``: scan → parse → persist
an exact storage reference → store (DEC-BREC-6, SYNTHESIS §3.3). The scan happens at
the router edge (where the
UploadFile lives) via ``validate_upload_file`` with a csv/pdf allow-list; this service
takes the resulting ``ValidatedUpload`` and: (1) parses — CSV deterministically
(utf-8-sig + stdlib ``csv``; schedule_import.py:224) or PDF via
``agreement_extractor.extract_text`` → LLM ``predict_text_result`` as an UNCONFIRMED proposal
(scanned/image PDF → honest empty/error, NEVER a fabricated row), and (3) persists a
``BankStatementImport`` parent + N ``BankFeedItem`` rows with ``source="statement_upload"``,
realm-scoped under FORCE RLS (the caller pins the realm GUC via ``check_tenant_access``).
Only a parsed, non-duplicate sync statement is stored; rejected bytes therefore never
become an unaddressable object.

Owns NO router — the POST endpoint is assembled centrally (thin-router/fat-service).
The LLM / scanner / object-storage boundaries are the ones mocked in tests.
"""
from __future__ import annotations

import csv
import hashlib
import io
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..redaction import redact_to_string
from ..upload_security import ValidatedUpload
from ...config import settings
from ...models import BankFeedItem, BankStatementImport, OrganizationCompany
from .parser_types import ImportLifecycleStatus, ParserIdentity
from .ingest_dedupe import dedupe_lines_for_persist

logger = logging.getLogger(__name__)

# Bank-statement upload allow-list (AC-1.1 #1): csv + pdf ONLY. Deliberately
# narrower than DOCUMENT_UPLOAD_* (which also admits docx/xlsx/txt) — a bank
# statement is a CSV export or a PDF, nothing else. Both content-type variants
# banks/browsers emit for CSV are included; validate_upload_file cross-checks the
# extension↔content-type match (upload_security.py:397).
BANK_STATEMENT_CONTENT_TYPES = frozenset(
    {
        "application/pdf",
        "text/csv",
        "text/plain",           # some browsers label a .csv as text/plain
        "application/octet-stream",  # generic binary fallback (extension still gated)
    }
)
BANK_STATEMENT_EXTENSIONS = frozenset({"csv", "pdf"})

MAX_STATEMENT_MB = 20  # mirrors documents.py MAX_SIZE_MB


# ─────────────────────────────────────────────────────────────────────────────
# Result types (plain data — the ORM row never crosses back to the router)
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ParsedLine:
    """One parsed statement line (pre-persist, engine-neutral)."""
    txn_date: date
    amount: Decimal
    description: str
    txn_type: str  # "DEBIT" | "CREDIT"


@dataclass(frozen=True)
class DroppedLine:
    """A dated CSV row that did not become a statement line (1-based CSV line number)."""
    row_number: int
    # "no_amount" | "unreadable_amount" | "ambiguous_debit_credit" | "unreadable_date"
    # | "unknown_direction"
    reason: str


@dataclass
class IngestResult:
    """The outcome of one statement import (returned to the router as plain data).

    ``line_count`` semantics (AC-M1 leg 5): BEFORE persist (the parse results)
    it is the number of PARSED statement lines; AFTER ``persist`` it is the
    number of rows NEWLY persisted by THIS import — post count-aware dedupe —
    which is LESS than the parsed count on a re-import (0 on a fully-idempotent
    one) and 0 on a batch-guard rejection. The parsed count is always
    ``len(lines)``. The ``bank_statement_imports`` table stores NO line count
    and ``bank_feed_items`` carries no import-batch FK, so a batch's own line
    count is not derivable retroactively — this returned figure is the
    authoritative per-import count, at import time only.
    """
    import_id: int | None
    source: str                       # "csv" | "pdf"
    unconfirmed: bool                 # True for PDF (LLM proposal), False for deterministic CSV
    line_count: int                   # rows NEWLY persisted this import (see docstring)
    opening_balance: Decimal | None
    closing_balance: Decimal | None
    storage_key: str | None
    status: str                       # "parsed" | "error" | "duplicate"
    error: str | None = None          # HONEST no-data / unreadable / duplicate message; None on success
    lines: list[ParsedLine] = field(default_factory=list)
    # Dated CSV rows that did not become lines (counted, never silently lost).
    dropped_lines: list[DroppedLine] = field(default_factory=list)

    @property
    def dropped_line_count(self) -> int:
        return len(self.dropped_lines)


def _error_result(
    source: str,
    error: str,
    *,
    unconfirmed: bool = False,
    opening: Decimal | None = None,
    closing: Decimal | None = None,
    storage_key: str | None = None,
) -> IngestResult:
    """Honest no-data / unreadable outcome: zero rows, a clear message, nothing persisted."""
    return IngestResult(
        import_id=None, source=source, unconfirmed=unconfirmed, line_count=0,
        opening_balance=opening, closing_balance=closing, storage_key=storage_key,
        status="error", error=error,
    )


def _parsed_result(
    source: str,
    lines: list[ParsedLine],
    *,
    unconfirmed: bool,
    opening: Decimal | None,
    closing: Decimal | None,
) -> IngestResult:
    """A successful (pre-persist) parse outcome — import_id/storage_key set by persist()."""
    return IngestResult(
        import_id=None, source=source, unconfirmed=unconfirmed, line_count=len(lines),
        opening_balance=opening, closing_balance=closing, storage_key=None,
        status="parsed", lines=lines,
    )


# ─────────────────────────────────────────────────────────────────────────────
# CSV parsing (deterministic — DEC-BREC-6 PRIMARY)
# ─────────────────────────────────────────────────────────────────────────────

# Header keys are matched as whole WORD phrases (camelCase split, punctuation
# ignored), never as substrings: "Value Date" must not read as a "value"
# (amount) column, nor "Debit Amount" as the signed-amount column.
_DATE_COL_KEYS = ("date", "posted", "transaction date", "post date", "posting date")
_SETTLEMENT_DATE_KEYS = ("value date",)  # used only when no booking-date column exists
_DESC_COL_KEYS = ("description", "memo", "details", "narrative", "payee", "name")
_AMOUNT_COL_KEYS = ("amount", "value")
_DEBIT_COL_KEYS = ("debit", "debits", "withdrawal", "withdrawals", "money out", "paid out")
_CREDIT_COL_KEYS = ("credit", "credits", "deposit", "deposits", "money in", "paid in")
_INDICATOR_DEBIT_WORDS = ("dr", *_DEBIT_COL_KEYS)
_INDICATOR_CREDIT_WORDS = ("cr", *_CREDIT_COL_KEYS)
# A header carrying one of these is never a money column (a date, a running balance).
_NOT_MONEY_COL_KEYS = ("date", "balance")
_OPENING_KEYS = ("opening balance", "beginning balance", "starting balance")
_CLOSING_KEYS = ("closing balance", "ending balance", "statement balance")

_DATE_FORMATS = (
    "%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%m/%d/%y", "%d/%m/%y",
    "%m-%d-%Y", "%d-%m-%Y", "%Y/%m/%d", "%b %d, %Y", "%d %b %Y",
)

# One money value: optional sign / currency symbol, comma-grouped or plain digits,
# at most two decimals, optional trailing minus and DR/CR marker. Anything else
# (a date, a European "1.234,56", "12.345", free text) does not match and is
# refused, never coerced into a number.
_MONEY_RE = re.compile(
    r"""
    ^(?P<lead>[+-])?
    [$£€]?
    (?P<lead2>[+-])?
    (?P<num>(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?|\.\d{1,2})
    (?P<trail>-)?
    (?P<marker>DR|CR)?$
    """,
    re.VERBOSE | re.IGNORECASE,
)
_CURRENCY_CODE_RE = re.compile(r"^(?:USD|CAD|GBP|EUR|AUD)\s*|\s*(?:USD|CAD|GBP|EUR|AUD)$", re.IGNORECASE)

# DroppedLine.reason vocabulary (stable strings).
DROP_NO_AMOUNT = "no_amount"
DROP_UNREADABLE_AMOUNT = "unreadable_amount"
DROP_AMBIGUOUS_DEBIT_CREDIT = "ambiguous_debit_credit"
DROP_UNREADABLE_DATE = "unreadable_date"
DROP_UNKNOWN_DIRECTION = "unknown_direction"

# A date-column cell that looks like a date (digits split by / . -, optionally with a
# time part) but is not one of _DATE_FORMATS; such a row is refused rather than
# skipped like a text footer.
_DATE_SHAPED_RE = re.compile(r"^\d{1,4}[./-]\d{1,2}[./-]\d{1,4}(?:[T\s].*)?$")


def _norm(cell: str | None) -> str:
    return (cell or "").strip().lower()


def _parse_money(raw: str | None) -> tuple[Decimal, bool] | None:
    """Parse one money string → ``(value, explicitly_signed)``; None when unreadable.

    Accepts ``$1,234.56``, ``(45.00)`` / ``($45.00)`` (parens = negative), a leading
    or trailing minus (``1,234.56-``), and a ``DR`` (money out) / ``CR`` (money in)
    marker. Conflicting markers (``-45.00 CR``, ``(45.00)-``) are unreadable.
    """
    if raw is None:
        return None
    s = raw.strip()
    if not s:
        return None
    parens = s.startswith("(") and s.endswith(")")
    if parens:
        s = s[1:-1].strip()
    s = _CURRENCY_CODE_RE.sub("", s)
    s = re.sub(r"\s+", "", s)
    m = _MONEY_RE.match(s)
    if m is None:
        return None
    signs = [g for g in (m.group("lead"), m.group("lead2"), m.group("trail")) if g]
    if len(signs) > 1 or (parens and signs):
        return None
    marker = (m.group("marker") or "").upper()
    negative = parens or signs == ["-"]
    if marker == "DR":
        if signs == ["+"]:
            return None
        negative = True
    elif marker == "CR" and negative:
        return None
    try:
        value = Decimal(m.group("num").replace(",", ""))
    except InvalidOperation:
        return None
    if negative and value != 0:
        value = -value
    return value, bool(parens or signs or marker)


def _parse_amount(raw: str | None) -> Decimal | None:
    """Parse a money string to Decimal (see ``_parse_money``); None when unreadable."""
    parsed = _parse_money(raw)
    return parsed[0] if parsed is not None else None


def _parse_date(raw: str | None) -> date | None:
    if not raw:
        return None
    s = raw.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _header_tokens(cell: str | None) -> list[str]:
    spaced = re.sub(r"([a-z])([A-Z])", r"\1 \2", cell or "")
    return re.findall(r"[a-z]+", spaced.lower())


def _has_phrase(tokens: list[str], phrase: str) -> bool:
    words = phrase.split()
    width = len(words)
    return any(tokens[i:i + width] == words for i in range(len(tokens) - width + 1))


def _find_col(
    header: list[str],
    keys: tuple[str, ...],
    *,
    exclude: tuple[str, ...] = (),
) -> int | None:
    """Index of the first header cell containing one of ``keys`` as a word phrase,
    skipping any cell that also contains an ``exclude`` phrase."""
    for idx, cell in enumerate(header):
        tokens = _header_tokens(cell)
        if any(_has_phrase(tokens, ex) for ex in exclude):
            continue
        if any(_has_phrase(tokens, key) for key in keys):
            return idx
    return None


@dataclass(frozen=True)
class _CsvColumns:
    date: int
    desc: int | None
    amount: int | None
    debit: int | None
    credit: int | None
    indicator: int | None  # a combined "Debit/Credit" (DR/CR) marker column


def _detect_columns(row: list[str]) -> _CsvColumns | None:
    """The column map when ``row`` is the transaction header row, else None."""
    date_col = _find_col(row, _DATE_COL_KEYS, exclude=_SETTLEMENT_DATE_KEYS)
    if date_col is None:
        date_col = _find_col(row, _SETTLEMENT_DATE_KEYS)
    if date_col is None:
        return None
    amount_col = _find_col(
        row, _AMOUNT_COL_KEYS,
        exclude=_NOT_MONEY_COL_KEYS + _DEBIT_COL_KEYS + _CREDIT_COL_KEYS,
    )
    debit_col = _find_col(row, _DEBIT_COL_KEYS, exclude=_NOT_MONEY_COL_KEYS + _CREDIT_COL_KEYS)
    credit_col = _find_col(row, _CREDIT_COL_KEYS, exclude=_NOT_MONEY_COL_KEYS + _DEBIT_COL_KEYS)
    if amount_col is None and debit_col is None and credit_col is None:
        return None
    indicator_col = None
    for idx, cell in enumerate(row):
        tokens = _header_tokens(cell)
        if any(_has_phrase(tokens, w) for w in _INDICATOR_DEBIT_WORDS) and any(
            _has_phrase(tokens, w) for w in _INDICATOR_CREDIT_WORDS
        ):
            indicator_col = idx
            break
    return _CsvColumns(
        date=date_col,
        desc=_find_col(row, _DESC_COL_KEYS),
        amount=amount_col,
        debit=debit_col,
        credit=credit_col,
        indicator=indicator_col,
    )


def _cell(row: list[str], idx: int | None) -> str:
    if idx is None or idx >= len(row):
        return ""
    return (row[idx] or "").strip()


def _indicator_direction(value: str) -> int | None:
    """-1 / +1 for a single debit / credit marker word ("DR", "Credit", "D"), else None."""
    tokens = _header_tokens(value)
    if len(tokens) != 1:
        return None
    word = tokens[0]
    if word in ("d", *_INDICATOR_DEBIT_WORDS):
        return -1
    if word in ("c", *_INDICATOR_CREDIT_WORDS):
        return 1
    return None


def _row_amount(row: list[str], cols: _CsvColumns) -> tuple[Decimal | None, str | None]:
    """``(amount, None)`` for a readable row, else ``(None, drop_reason)``."""
    raw_amount = _cell(row, cols.amount)
    if raw_amount:
        parsed = _parse_money(raw_amount)
        if parsed is None:
            return None, DROP_UNREADABLE_AMOUNT
        amount, explicitly_signed = parsed
        direction = _indicator_direction(_cell(row, cols.indicator))
        if direction is not None and amount != 0:
            if not explicitly_signed:
                amount = abs(amount) * direction
            elif (amount < 0) != (direction < 0):
                return None, DROP_UNREADABLE_AMOUNT
        elif cols.indicator is not None and amount != 0 and not explicitly_signed:
            # The file carries direction in its Debit/Credit column; an unsigned amount
            # whose marker is blank or unrecognised has no direction to read.
            return None, DROP_UNKNOWN_DIRECTION
        return amount, None

    raw_debit, raw_credit = _cell(row, cols.debit), _cell(row, cols.credit)
    if not raw_debit and not raw_credit:
        return None, DROP_NO_AMOUNT
    parsed_debit = _parse_money(raw_debit) if raw_debit else None
    parsed_credit = _parse_money(raw_credit) if raw_credit else None
    if (raw_debit and parsed_debit is None) or (raw_credit and parsed_credit is None):
        return None, DROP_UNREADABLE_AMOUNT
    debit = parsed_debit[0] if parsed_debit else None
    credit = parsed_credit[0] if parsed_credit else None
    # The column is the direction. A debit value explicitly marked as money in
    # ("45.00 CR", "+45.00") or a negative credit value contradicts it.
    if (parsed_debit and parsed_debit[1] and parsed_debit[0] > 0) or (credit is not None and credit < 0):
        return None, DROP_UNREADABLE_AMOUNT
    # A zero in the unused column is a filler, not a second amount.
    if debit and credit:
        return None, DROP_AMBIGUOUS_DEBIT_CREDIT
    if debit:
        return -abs(debit), None  # a debit column value is money out
    if credit:
        return abs(credit), None
    return Decimal("0.00"), None


def _scan_balances(rows: list[list[str]]) -> tuple[Decimal | None, Decimal | None]:
    """Best-effort opening/closing balance scan over label:value rows above/below the table."""
    opening: Decimal | None = None
    closing: Decimal | None = None
    for row in rows:
        joined = " ".join(_norm(c) for c in row)
        for cell in row:
            amount = _parse_amount(cell)
            if amount is None:
                continue
            if opening is None and any(k in joined for k in _OPENING_KEYS):
                opening = amount
            if any(k in joined for k in _CLOSING_KEYS):
                closing = amount
    return opening, closing


def parse_csv(file_bytes: bytes) -> IngestResult:
    """Deterministic CSV parse (utf-8-sig + stdlib csv; schedule_import.py:224 precedent).

    A dated row whose amount cannot be read unambiguously refuses the WHOLE import
    (a statement missing lines cannot reconcile); a dated row with no amount at
    all is skipped. Both are reported in ``dropped_lines`` by CSV line number.
    """
    text = file_bytes.decode("utf-8-sig", errors="replace")
    all_rows = list(csv.reader(io.StringIO(text)))
    if not all_rows:
        return _error_result("csv", "Empty CSV: no rows found.")

    # Locate the header row: the first row that carries a date column AND an
    # amount-or-debit-or-credit column. Rows above it are statement metadata.
    header_idx: int | None = None
    cols: _CsvColumns | None = None
    for i, row in enumerate(all_rows):
        cols = _detect_columns(row)
        if cols is not None:
            header_idx = i
            break

    if header_idx is None or cols is None:
        return _error_result("csv", "Unrecognized CSV: no Date + Amount/Debit/Credit header found.")

    opening, closing = _scan_balances(all_rows[:header_idx] + all_rows[header_idx + 1:])

    lines: list[ParsedLine] = []
    dropped: list[DroppedLine] = []
    for offset, row in enumerate(all_rows[header_idx + 1:]):
        row_number = header_idx + 2 + offset  # 1-based CSV line number
        if not any(_norm(c) for c in row):
            continue  # blank line
        raw_date = _cell(row, cols.date)
        txn_date = _parse_date(raw_date)
        if txn_date is None:
            if _DATE_SHAPED_RE.match(raw_date):
                # A transaction row whose date is in an unsupported shape (31.03.2026,
                # 13/31/2026) is a lost line, not a footer.
                dropped.append(DroppedLine(row_number=row_number, reason=DROP_UNREADABLE_DATE))
            continue  # a subtotal / footer row without a date — skip, never fabricate

        amount, drop_reason = _row_amount(row, cols)
        if amount is None:
            dropped.append(DroppedLine(row_number=row_number, reason=drop_reason or DROP_NO_AMOUNT))
            continue  # never a $0 placeholder

        description = _cell(row, cols.desc)
        txn_type = "CREDIT" if amount >= 0 else "DEBIT"
        lines.append(ParsedLine(txn_date=txn_date, amount=amount,
                                description=description, txn_type=txn_type))

    refused = [d for d in dropped if d.reason != DROP_NO_AMOUNT]
    if refused:
        shown = ", ".join(str(d.row_number) for d in refused[:10])
        more = "" if len(refused) <= 10 else f" and {len(refused) - 10} more"
        result = _error_result(
            "csv",
            f"{len(refused)} statement row(s) have an amount or date that cannot be read "
            f"unambiguously (CSV lines {shown}{more}). Nothing was imported: correct "
            "those rows or export the statement again, then re-upload.",
            opening=opening, closing=closing,
        )
        result.dropped_lines = dropped
        return result
    if dropped:
        logger.info(
            "bank_statement_csv_rows_without_amount count=%d lines=%s",
            len(dropped), ",".join(str(d.row_number) for d in dropped[:20]),
        )

    if not lines:
        result = _error_result("csv", "No parseable transaction rows in CSV.",
                               opening=opening, closing=closing)
        result.dropped_lines = dropped
        return result

    result = _parsed_result("csv", lines, unconfirmed=False, opening=opening, closing=closing)
    result.dropped_lines = dropped
    return result


# ─────────────────────────────────────────────────────────────────────────────
# PDF parsing (LLM proposal — DEC-BREC-6; UNCONFIRMED, honest empty-state)
# ─────────────────────────────────────────────────────────────────────────────

_PDF_SYSTEM_PROMPT = (
    "You extract bank-statement transactions from raw statement text. Return a single "
    "JSON object: {\"opening_balance\": <number|null>, \"closing_balance\": <number|null>, "
    "\"lines\": [{\"date\": \"YYYY-MM-DD\", \"description\": \"...\", \"amount\": <number>}]}. "
    "amount is NEGATIVE for money out (debits/withdrawals) and POSITIVE for money in "
    "(credits/deposits). Only include rows you can read verbatim from the text. If the "
    "text contains no transactions, return {\"lines\": []}. NEVER invent a row."
)

# Bound on the statement text sent to the model in one pass. Over this the parse
# cannot be trusted as complete (the tail would be dropped), so we fail HONESTLY
# rather than silently truncate — see _PDF_TOO_LARGE_MSG.
_PDF_MAX_INPUT_CHARS = 50000

# Shown for BOTH silent-data-loss paths: an oversized input (raw_text over the cap)
# and an output-token-ceiling hit (the model stopped mid-statement → result.truncated).
# Either way the parse is a partial the user must not mistake for a complete import, so
# we persist NOTHING and give the honest remedy instead of "Could not read transactions."
_PDF_TOO_LARGE_MSG = (
    "This statement is too large to parse in one pass — split it by month, "
    "or upload a CSV export."
)


def parse_pdf(
    file_bytes: bytes,
    filename: str,
    *,
    timeout_override: float = 20,
    max_retries_override: int = 1,
    allow_fallback: bool = False,
) -> IngestResult:
    """PDF parse via extract_text → predict_text_result. Rows are an UNCONFIRMED proposal.

    Empty/scanned/image PDF (extract_text returns "") → honest error, zero rows.

    The LLM budget is a PARAMETER because the same parse runs under two very
    different deadlines (SYNTHESIS 20260716-pdf-import-async):

    - **Defaults = the INLINE budget** (20s / 1 retry / no fallback). Kept for any
      caller still on the request path, where a struggling provider must fail in
      ~20-40s rather than the ~3-6 min the shared client's 45s x 3 + fallback takes.
    - **The WORKER passes a LONG budget** (``timeout_override=240``). Prod telemetry:
      a real statement parse runs 40-183s (avg 135.8s on the timing-out calls) vs
      ~2.5s for a normal gpt-4o call — it is output-token-bound, not stuck. A worker
      has NO request to time out, so failing fast there would kill EVERY real
      statement parse; one long attempt is the CORRECT budget.

    On failure ``predict_text_result`` returns a result whose ``.content`` is None and
    the non-dict branch below lands on the EXISTING honest-error path (never a fabricated
    row). A ``.truncated`` result (the model hit its output-token ceiling) OR an oversized
    ``raw_text`` returns the honest "too large" error and persists nothing — the two
    silent-data-loss paths this parse now fails loudly on.
    """
    # Lazy imports: agreement_extractor pulls pdfplumber, llm pulls the provider
    # adapters — both heavy; defer to call time (also keeps the test seam patchable).
    from ..agreement_extractor import extract_text
    from ..llm import _safe_json_loads, analysis_llm

    raw_text = extract_text(file_bytes, filename)
    if not raw_text or not raw_text.strip():
        return _error_result(
            "pdf",
            "No text could be extracted from this PDF. It looks like a scanned or "
            "image-only statement (no OCR is available). Upload a CSV export or a "
            "text-based PDF instead.",
            unconfirmed=True,
        )

    # HONEST oversized-input guard: the old code did ``raw_text[:50000]``, silently
    # dropping the tail of a large statement and persisting the partial as if it were
    # the whole thing. Fail loud instead — never drop rows without telling the user.
    if len(raw_text) > _PDF_MAX_INPUT_CHARS:
        return _error_result("pdf", _PDF_TOO_LARGE_MSG, unconfirmed=True)

    # The per-call budget is the caller's to choose (see the docstring): the inline
    # default fails fast, the worker passes a long single attempt. Fix A's llm.py
    # override machinery (commit 49166c0c) is REUSED here, not reverted. We read the
    # full LLMCallResult (predict_text_result, not predict_json) so we can branch on
    # ``.truncated`` — an output-token-ceiling hit predict_json would silently swallow.
    result = analysis_llm.predict_text_result(
        system_prompt=_PDF_SYSTEM_PROMPT,
        user_prompt=raw_text,
        temperature=0.0,
        model=None,
        json_mode=True,
        allow_fallback=allow_fallback,
        timeout_override=timeout_override,
        max_retries_override=max_retries_override,
    )
    # HONEST output-truncation guard: the model stopped at its output-token ceiling
    # mid-statement, so the content is a partial that would lose the tail rows on
    # persist. Persist NOTHING — the same "too large" remedy as the oversized input.
    if result.truncated:
        return _error_result("pdf", _PDF_TOO_LARGE_MSG, unconfirmed=True)

    # Mirror predict_json's JSON handling (llm.py:278-291): a dict is used as-is; empty
    # content or genuinely-unparseable output (None) lands on the EXISTING honest-error
    # branch below — never a fabricated row.
    parsed = _safe_json_loads(result.content) if result.content else None
    if not isinstance(parsed, dict):
        return _error_result(
            "pdf", "Could not read transactions from this PDF. Please review or upload a CSV.",
            unconfirmed=True,
        )

    opening = _coerce_decimal(parsed.get("opening_balance"))
    closing = _coerce_decimal(parsed.get("closing_balance"))

    raw_lines = parsed.get("lines")
    lines: list[ParsedLine] = []
    if isinstance(raw_lines, list):
        for item in raw_lines:
            if not isinstance(item, dict):
                continue
            # dict field reads (NOT DB queries) — pull the raw values first, then parse.
            raw_date = str(item["date"]) if "date" in item else ""
            raw_amount = item["amount"] if "amount" in item else None
            raw_desc = item["description"] if "description" in item else ""
            txn_date = _parse_date(raw_date)
            amount = _coerce_decimal(raw_amount)
            if txn_date is None or amount is None:
                continue  # honest: drop an unreadable proposed row, never fabricate
            description = str(raw_desc or "").strip()
            txn_type = "CREDIT" if amount >= 0 else "DEBIT"
            lines.append(ParsedLine(txn_date=txn_date, amount=amount,
                                    description=description, txn_type=txn_type))

    if not lines:
        return _error_result(
            "pdf", "No transactions found in this PDF. Please review or upload a CSV.",
            unconfirmed=True, opening=opening, closing=closing,
        )

    return _parsed_result("pdf", lines, unconfirmed=True, opening=opening, closing=closing)


def _coerce_decimal(value: object) -> Decimal | None:
    """Coerce an opaque JSON value (int/float/str/None) from the LLM proposal to Decimal."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return Decimal(str(value))
        except InvalidOperation:
            return None
    if isinstance(value, str):
        return _parse_amount(value)
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Store + persist
# ─────────────────────────────────────────────────────────────────────────────

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_storage_name(filename: str) -> str:
    """Sanitize a filename for the trailing segment of an object key (documents.py:181)."""
    name = (filename or "statement").rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    name = _SAFE_NAME_RE.sub("_", name).strip("._") or "statement"
    return name[:120]


def _statement_storage_key(org_id: int, upload: ValidatedUpload) -> str:
    safe_name = _safe_storage_name(upload.filename)
    return f"tenants/{org_id}/bank-statements/{uuid4()}/{safe_name}"


def store_original(
    org_id: int,
    upload: ValidatedUpload,
    *,
    storage_key: str | None = None,
) -> str | None:
    """Store the original statement under a tenant-prefixed key. None = not stored.

    ``put_object`` is unconfigured-safe (returns None → storage off/failed): the
    caller leaves ``storage_key`` NULL and continues — NEVER fail the upload
    (object_storage.py:87 contract; documents.py:325 mirror). Only store when the
    scan did not quarantine/fail (status in {clean, skipped}); a fail-closed scanner
    already raised before we got here.
    """
    from .. import object_storage

    if upload.scan_status not in {"clean", "skipped"}:
        return None
    key = storage_key or _statement_storage_key(org_id, upload)
    stored = object_storage.put_object(key, upload.content, upload.content_type or None)
    # The object-store contract writes the exact caller-selected key.  Return that
    # durable identity rather than trusting an adapter echo to substitute a key.
    return key if stored is not None else None


def fetch_original(storage_key: str | None) -> bytes | None:
    """Read back the stored original's bytes. None = not retrievable (never raises).

    The read twin of ``store_original``, and the seam the ASYNC worker lane stands
    on: the Celery task is a separate process that only receives the import id, so
    the scanned bytes it parses come from object storage, not the request.

    Returns None — never raises — when storage is unconfigured, the key is NULL
    (``put_object`` is unconfigured-safe, so a stored key is never guaranteed), or
    the object is missing/unreadable. The caller turns None into an HONEST terminal
    error; it must never be mistaken for "an empty statement".

    Note (scope): the S3 verb lives here rather than as an ``object_storage.get_object``
    because ``app/services/object_storage.py`` is outside plan 20260716-pdf-import-async's
    ratified ``surface_scope``. ``object_storage`` already owns the client + bucket
    resolution (``_client()`` mirrors ``put_object``/``delete_prefix``); promoting this
    to a public ``get_object`` is the follow-on cleanup.
    """
    if not storage_key:
        return None
    from .. import object_storage

    client = object_storage._client()
    if client is None:
        return None
    try:
        response = client.get_object(Bucket=settings.object_storage_bucket, Key=storage_key)
        return response["Body"].read()
    except Exception as exc:  # noqa: BLE001 - boto3 raises per-error client classes; degrade to None
        logger.warning(
            "bank statement original fetch failed key=%s: %s",
            storage_key, redact_to_string(exc),
        )
        return None


def create_import_header(
    db: Session,
    *,
    realm_id: str,
    org_id: int,
    account_id: str,
    account_name: str | None,
    upload: ValidatedUpload,
    force_reimport: bool = False,
    prepare_resumable: bool = False,
) -> BankStatementImport:
    """Store a scanned PDF and optionally persist its resumable work manifest.

    ``prepare_resumable=True`` is the v2 HTTP cutover seam: deterministic extraction
    creates the stable chunk and candidate descriptors that must commit before any
    broker publish; ambiguous spans remain staged for the asynchronous worker. No
    provider call happens here. The default remains the lightweight header-only seam
    for existing direct callers and the registered legacy-payload upgrade path.

    Session discipline: this FLUSHES (assigning ``.id``) and does not commit — the
    service joins the caller's transaction and the router owns the request-lifecycle
    commit boundary (the ``upsert_clean_start`` twin). The router's
    ``_create_import_header_committed`` helper then commits BEFORE enqueueing, which is
    load-bearing: the worker looks the row up by id in another process, so an
    uncommitted row would be invisible to it (the enqueue-before-commit race).

    ``storage_key`` may be None — ``put_object`` is unconfigured-safe and NEVER fails
    the upload (object_storage.py:87). The row is still created: the worker then fails
    it to an HONEST terminal ``error`` (it has no bytes to parse), which is strictly
    better than a 500 on a request whose file was already scanned + accepted.

    ``force_reimport`` is STAMPED onto the row, and that row is the flag's ONLY channel to
    the worker — the router enqueues just ``.delay(import_id)``, so nothing else survives
    the hand-off. Without it a deliberate PDF re-import dead-ends: the batch-level guard
    fires and tells the user to "retry with force_reimport", which is what they already
    did. An unforced call stamps ``False`` (legacy pre-migration rows read ``NULL``); both
    are falsy, so the batch guard still runs by default. The count-aware LINE-level guard
    (the correctness leg that stops the B4-1 doubling) is independent of the flag and
    always runs on BOTH lanes — a forced re-import tops up missing lines, never doubles.
    See the module header in ``app/tasks/bank_import.py``.
    """
    storage_key = store_original(org_id, upload)
    batch = BankStatementImport(
        realm_id=realm_id,
        account_id=account_id,
        account_name=account_name,
        source="pdf",
        storage_key=storage_key,
        status="parsing",
        # CARRY the force-reimport intent to the worker: the router enqueues only
        # `.delay(import_id)`, so the row is the ONLY channel. Without it a deliberate
        # re-import dead-ends on the batch-level duplicate guard telling the user to
        # "retry with force_reimport" — which they already did.
        force_reimport=force_reimport,
    )
    if prepare_resumable:
        identity = ParserIdentity.current(hashlib.sha256(upload.content).hexdigest())
        batch.source_sha256 = identity.source_sha256
        batch.parser_version = identity.parser_version
        batch.extractor_version = identity.extractor_version
        batch.prompt_schema_version = identity.prompt_schema_version
        batch.lifecycle_status = ImportLifecycleStatus.QUEUED.value
        batch.lineage_status = "controlled"
    db.add(batch)
    db.flush()  # assign batch.id before returning it
    if not prepare_resumable:
        return batch
    organization_company = db.execute(
        select(OrganizationCompany).where(
            OrganizationCompany.organization_id == org_id,
            OrganizationCompany.realm_id == realm_id,
        )
    ).scalar_one_or_none()
    if organization_company is None:
        raise ValueError("realm is not mapped to the supplied organization")

    # Runtime import avoids a module cycle: parser_persist reads fetch_original
    # only when a worker reconstructs retained bytes.
    from .parser_persist import SourceIdentityError, prepare_resumable_import

    try:
        with db.begin_nested():
            prepare_resumable_import(
                db,
                statement_import_id=batch.id,
                realm_id=realm_id,
                organization_id=org_id,
                organization_company_id=organization_company.id,
                source_bytes=upload.content,
            )
    except SourceIdentityError:
        # The scanned upload remains a durable, visible review item.  Extraction
        # failure is not permission to fabricate an empty manifest or make the
        # feature disappear behind a request 500.
        batch.lifecycle_status = ImportLifecycleStatus.REVIEW_REQUIRED.value
        batch.status = "error"
        batch.review_reason_code = "source_identity_invalid"
        batch.review_action = "reupload"
        batch.error_detail = (
            "The statement could not be reconstructed from the uploaded PDF; "
            "re-upload the original file."
        )
        db.flush()
    return batch


def find_duplicate_import(
    db: Session,
    *,
    realm_id: str,
    account_id: str,
    period_start: date | None,
    period_end: date | None,
    closing_balance: Decimal | None,
    exclude_import_id: int | None = None,
) -> BankStatementImport | None:
    """The batch-level statement-identity lookup (AC-M1 leg 3).

    A prior non-error import for the SAME realm+account whose
    (period_start, period_end, closing_balance) all match is the same
    statement re-uploaded/re-parsed — the across-requests doubling shape the
    crit-eval Q4 query confirmed as the historical B4-1 mechanism (two batches,
    same account/period/closing balance, different storage keys, a day apart).

    Identity choice (no-migration, justified): closing_balance is REQUIRED for
    a match — period boundaries alone are weak identity (two balance-less CSVs
    covering the same date range are not provably the same statement), so a
    ``closing_balance IS NULL`` parse skips this guard and relies on the
    count-aware line-level guard, which already makes a duplicate re-import
    +0. A file-hash column was deliberately NOT added: Q4 showed the two
    historical batches had DIFFERENT storage keys (plausibly different bytes —
    a re-export), a shape a byte-hash misses but this parse-identity catches;
    byte-identical re-uploads parse to the same identity, so the hash leg is
    subsumed. No migration also avoids an Alembic head race with the sibling
    M2 migration this wave.

    realm_id== + account_id== stay explicit (app-layer predicate is primary;
    RLS backstops — multi-tenancy.md).

    ``exclude_import_id`` omits ONE row from the lookup, and is load-bearing on the
    async lane: there the batch row is created UP FRONT (``create_import_header``) and
    the worker finalizes it IN PLACE, so once it carries the parsed identity it would
    match itself. A Celery redelivery re-parsing that row would then see its OWN
    successful import as a "duplicate" and flip it to ``error`` — corrupting a good
    import on a benign retry. The worker passes its header id here.
    """
    if closing_balance is None or period_start is None or period_end is None:
        return None
    query = db.query(BankStatementImport).filter(
        BankStatementImport.realm_id == realm_id,
        BankStatementImport.account_id == account_id,
        BankStatementImport.period_start == period_start,
        BankStatementImport.period_end == period_end,
        BankStatementImport.closing_balance == closing_balance,
        BankStatementImport.status != "error",
    )
    if exclude_import_id is not None:
        query = query.filter(BankStatementImport.id != exclude_import_id)
    return query.order_by(BankStatementImport.id.desc()).first()


def _lock_sync_import_scope(
    db: Session,
    *,
    organization_id: int,
    realm_id: str,
) -> None:
    """Serialize the sync CSV identity+multiset decision on real PostgreSQL.

    Two first-time uploads otherwise both observe an empty identity/multiset and
    commit duplicate imports and feed rows.  The existing realm mapping is a stable,
    RLS-excluded lock row; the lock is held only through the database commit and is
    released before object-storage I/O.  SQLite remains a unit-test substrate and
    cannot prove ``FOR UPDATE`` semantics; the FORCE-RLS PostgreSQL lane does.
    """
    if db.get_bind().dialect.name != "postgresql":
        return
    mapping_id = db.execute(
        select(OrganizationCompany.id)
        .where(
            OrganizationCompany.organization_id == organization_id,
            OrganizationCompany.realm_id == realm_id,
        )
        .with_for_update()
    ).scalar_one_or_none()
    if mapping_id is None:
        raise ValueError("realm is not mapped to the supplied organization")


def _duplicate_result(
    result: IngestResult,
    duplicate: BankStatementImport,
    *,
    storage_key: str | None,
) -> IngestResult:
    """Build the single teaching envelope used by both duplicate guard locations."""
    period_start = min((line.txn_date for line in result.lines), default=None)
    period_end = max((line.txn_date for line in result.lines), default=None)
    if period_start is None or period_end is None:
        raise ValueError("duplicate statement identity requires a non-empty period")
    imported_on = (
        duplicate.created_at.date().isoformat()
        if duplicate.created_at is not None
        else "an earlier date"
    )
    return IngestResult(
        import_id=None,
        source=result.source,
        unconfirmed=result.unconfirmed,
        line_count=0,
        opening_balance=result.opening_balance,
        closing_balance=result.closing_balance,
        storage_key=storage_key,
        status="duplicate",
        dropped_lines=list(result.dropped_lines),
        error=(
            "Duplicate statement: this account already has an import covering "
            f"{period_start.isoformat()} to {period_end.isoformat()} "
            f"with the same closing balance (import #{duplicate.id}, imported "
            f"{imported_on}). Nothing was imported. If this is a corrected "
            "statement or a deliberate re-import (e.g. the duplicate-line repair "
            "runbook), retry with force_reimport — the count-aware line guard "
            "adds only the missing lines, never duplicates."
        ),
    )


def persist(
    db: Session,
    *,
    realm_id: str,
    account_id: str,
    account_name: str | None,
    result: IngestResult,
    storage_key: str | None,
    force_reimport: bool = False,
    batch: BankStatementImport | None = None,
) -> IngestResult:
    """Persist the BankStatementImport parent + N statement-source BankFeedItem rows.

    ONE canonical parse→persist path serves BOTH lanes (plan 20260716-pdf-import-async
    AC-2) — the guards, the count-aware dedupe, and the BankFeedItem writes below are
    shared, never forked. ``batch`` selects only WHERE the parent row comes from:

    - ``batch=None`` (the SYNC CSV lane, ``ingest_statement``) — CREATE the parent here,
      exactly as before. Unchanged behavior.
    - ``batch=<header>`` (the ASYNC PDF lane, the Celery worker) — FINALIZE the
      ``parsing`` header ``create_import_header`` already opened, in place. It must be
      the same row, because its id is what the POST returned and the UI is polling; a
      second row would strand the poller on ``parsing`` forever.

    Either way the parent's data fields and the child rows are written in ONE
    transaction and committed together, so the worker can never leave rows behind a
    non-terminal header (a crash before the commit rolls BOTH back and the re-run
    re-parses from a clean slate).

    Realm-scoped: the caller MUST have pinned the realm GUC before calling — every
    write below lands under FORCE RLS on the realm axis (tenant_rls.py). ``realm_id``
    is session-derived, never client-supplied.

    Two guards, in order (AC-M1):
    1. BATCH statement-identity guard (``find_duplicate_import``) — a re-uploaded/
       re-parsed statement is rejected BEFORE any row is written, returning an
       honest ``status="duplicate"`` teaching result. ``force_reimport=True`` is
       the audited doorway PAST this guard only (the runbook repair path —
       docs/runbooks/bank-statement-reimport.md); it never bypasses guard 2.
    2. COUNT-AWARE line guard (``dedupe_lines_for_persist``) — always runs; per
       key it persists max(0, parsed − existing), so even a forced re-import
       adds only the missing occurrences, never duplicates.

    The batch row's period_start/period_end store the PARSED statement's date
    range (min/max of ``result.lines``), NOT the post-dedupe subset — the row
    describes the statement's coverage, and the stored identity must match what
    a future ``find_duplicate_import`` computes from a fresh parse. The returned
    ``line_count`` is the NEWLY persisted row count (see IngestResult docstring).
    """
    parsed_period_start = min((ln.txn_date for ln in result.lines), default=None)
    parsed_period_end = max((ln.txn_date for ln in result.lines), default=None)

    if not force_reimport:
        duplicate = find_duplicate_import(
            db, realm_id=realm_id, account_id=account_id,
            period_start=parsed_period_start, period_end=parsed_period_end,
            closing_balance=result.closing_balance,
            # Never let an in-place-finalized header match ITSELF on a re-parse.
            exclude_import_id=batch.id if batch is not None else None,
        )
        if duplicate is not None:
            return _duplicate_result(
                result,
                duplicate,
                storage_key=storage_key,
            )

    lines_to_persist = dedupe_lines_for_persist(db, lines=result.lines, realm_id=realm_id, account_id=account_id)
    period_start = parsed_period_start
    period_end = parsed_period_end

    if batch is None:
        batch = BankStatementImport(
            realm_id=realm_id,
            account_id=account_id,
            account_name=account_name,
            period_start=period_start,
            period_end=period_end,
            source=result.source,
            storage_key=storage_key,
            opening_balance=result.opening_balance,
            closing_balance=result.closing_balance,
            status="parsed",
        )
        db.add(batch)
    else:
        # Finalize the async lane's `parsing` header IN PLACE (same row, same id the
        # UI is polling). account_id/realm_id/storage_key were fixed at header time
        # and are deliberately not rewritten here.
        batch.account_name = account_name
        batch.period_start = period_start
        batch.period_end = period_end
        batch.source = result.source
        batch.opening_balance = result.opening_balance
        batch.closing_balance = result.closing_balance
        batch.status = "parsed"
    # The count the CLIENT is told (ImportStatusOut/ImportResultOut): rows NEWLY
    # persisted by THIS import, post count-aware dedupe — never the parsed total,
    # which would over-report a partial top-up as a full import.
    batch.line_count = len(lines_to_persist)
    batch.error_detail = None
    db.flush()  # assign batch.id before returning it

    for ln in lines_to_persist:
        db.add(
            BankFeedItem(
                realm_id=realm_id,
                source="statement_upload",  # the C0-a discriminator (models.py:2085)
                qbo_txn_id=None,            # a statement line has no QBO BankTransaction.Id
                txn_date=ln.txn_date,
                amount=ln.amount,
                description=ln.description,
                txn_type=ln.txn_type,       # DEBIT | CREDIT
                bank_account_ref=account_id,
                bank_account_name=account_name,
                status="pending",
            )
        )

    db.commit()

    return IngestResult(
        import_id=batch.id,
        source=result.source,
        unconfirmed=result.unconfirmed,
        line_count=len(lines_to_persist),
        opening_balance=result.opening_balance,
        closing_balance=result.closing_balance,
        storage_key=storage_key,
        status="parsed",
        lines=lines_to_persist,
        dropped_lines=list(result.dropped_lines),
    )


def ingest_statement(
    db: Session,
    *,
    realm_id: str,
    org_id: int,
    account_id: str,
    account_name: str | None,
    upload: ValidatedUpload,
    force_reimport: bool = False,
) -> IngestResult:
    """End-to-end: (already-scanned) upload → parse → persist exact key → store.

    The scan happened at the edge (``validate_upload_file`` inside the router, which
    is where the UploadFile lives). This service parses first so malformed or known-
    duplicate bytes are never stored. For a valid statement, it serializes the
    database identity/multiset decision, commits the import with an exact planned
    object key, and only then writes the object. The object therefore never exists
    without a durable row that identifies its exact key.

    On a parse error (empty CSV / scanned PDF / no rows) NOTHING is persisted and the
    honest error result is returned (zero rows, never a fabricated line). A statement
    whose identity matches a prior import is likewise persisted NOWHERE and returned
    as ``status="duplicate"`` with a teaching message; ``force_reimport=True`` is the
    deliberate doorway past that batch guard (the count-aware line guard still runs —
    see ``persist``).
    """
    ext = (upload.extension or "").lower()
    if ext == "csv":
        parsed = parse_csv(upload.content)
    elif ext == "pdf":
        parsed = parse_pdf(upload.content, upload.filename)
    else:
        # validate_upload_file's allow-list should preclude this; fail honest, not silent.
        return _error_result(
            ext or "unknown", f"Unsupported statement type: .{ext or 'unknown'}",
        )

    if parsed.status == "error":
        # Surface the honest no-data/unreadable result; persist nothing.
        refused = _error_result(
            parsed.source, parsed.error or "Could not parse statement.",
            unconfirmed=parsed.unconfirmed, opening=parsed.opening_balance,
            closing=parsed.closing_balance,
        )
        refused.dropped_lines = list(parsed.dropped_lines)
        return refused

    _lock_sync_import_scope(
        db,
        organization_id=org_id,
        realm_id=realm_id,
    )
    if not force_reimport:
        duplicate = find_duplicate_import(
            db,
            realm_id=realm_id,
            account_id=account_id,
            period_start=min(
                (line.txn_date for line in parsed.lines),
                default=None,
            ),
            period_end=max(
                (line.txn_date for line in parsed.lines),
                default=None,
            ),
            closing_balance=parsed.closing_balance,
        )
        if duplicate is not None:
            db.rollback()  # release the serialization lock; this path wrote nothing
            return _duplicate_result(parsed, duplicate, storage_key=None)

    planned_storage_key = _statement_storage_key(org_id, upload)
    persisted = persist(
        db,
        realm_id=realm_id,
        account_id=account_id,
        account_name=account_name,
        result=parsed,
        storage_key=planned_storage_key,
        force_reimport=force_reimport,
    )
    if persisted.status == "duplicate":
        # Defensive compatibility for non-Postgres test substrates and any caller
        # that supplies its own transaction discipline: no object has been written.
        db.rollback()
        persisted.storage_key = None
        return persisted

    storage_key = store_original(
        org_id,
        upload,
        storage_key=planned_storage_key,
    )
    if storage_key is None:
        # Storage is optional. Clear the planned pointer when the write did not
        # happen; if this bookkeeping commit itself fails, the durable row still
        # identifies only a missing exact key—never an untracked retained object.
        batch = db.execute(
            select(BankStatementImport).where(
                BankStatementImport.id == persisted.import_id,
                BankStatementImport.realm_id == realm_id,
                BankStatementImport.storage_key == planned_storage_key,
            )
        ).scalar_one_or_none()
        if batch is not None:
            batch.storage_key = None
            db.commit()
        persisted.storage_key = None
    return persisted

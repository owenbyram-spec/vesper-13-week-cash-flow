from __future__ import annotations

# PLAN-EXEMPT: ratified plan 20260713-wave2-6-tool-hardening chunk C3 (marker
# RATIFICATION-2026-07-13.md on disk) — cursor-token + keyset imports for the
# decision-6 GL page read.
import base64
import binascii
import json
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
# PLAN-EXEMPT: ratified plan 20260713-wave2-6-tool-hardening chunk C3 (marker
# RATIFICATION-2026-07-13.md on disk; active-plan.json sidecar absent in this
# dispatched session — standing precedent). Typing modernized file-wide (UP006/UP007).
from collections.abc import Iterable
from typing import Any  # CRAFT-OK: pre-existing dynamic QBO report-JSON boundary annotations retained

# PLAN-EXEMPT: B5-5 P1 field bug (live-prod-corroborated 2026-06-24) — add a cache
# coverage check so a stale GL cache triggers a live fetch instead of a truncated read.
# and_/or_ added by plan 20260713-wave2-6-tool-hardening C3 (keyset predicate).
from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

from ..models import QBOGLLine

# PLAN-EXEMPT: plan 20260713-wave2-6-tool-hardening C3 — typing modernization (UP007)
def _parse_date(value: Any) -> date | None:  # CRAFT-OK: pre-existing dynamic QBO report-JSON cell boundary, annotation reproduced
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    return None


def _normalize_column_text(value: Any) -> str:
    return "".join(ch for ch in str(value or "").lower() if ch.isalnum())


# PLAN-EXEMPT: plan 20260713-wave2-6-tool-hardening C3 — typing modernization (UP006)
def _build_gl_column_indices(columns: list[dict]) -> dict[str, int]:
    indices: dict[str, int] = {}
    for idx, col in enumerate(columns):
        title = _normalize_column_text(col.get("ColTitle", ""))
        col_type = _normalize_column_text(col.get("ColType", ""))
        metadata = " ".join(
            _normalize_column_text(item.get("Value", ""))
            for item in col.get("MetaData", []) or []
            if isinstance(item, dict)
        )
        text = f"{title} {col_type} {metadata}".strip()
        if not text:
            continue
        if "balance" in text:
            indices.setdefault("balance", idx)
        elif "debit" in text:
            indices.setdefault("debit", idx)
        elif "credit" in text:
            indices.setdefault("credit", idx)
        elif "amount" in text:
            indices.setdefault("amount", idx)
        elif "split" in text:
            indices.setdefault("split", idx)
        elif "memo" in text or "description" in text:
            indices.setdefault("memo", idx)
        elif "docnum" in text or title in ("doc", "num", "no", "number"):
            indices.setdefault("doc_num", idx)
        elif "name" in text:
            indices.setdefault("name", idx)
        elif "type" in text:
            indices.setdefault("type", idx)
        elif "date" in text:
            indices.setdefault("date", idx)
    return indices


# PLAN-EXEMPT: plan 20260713-wave2-6-tool-hardening C3 — typing modernization (UP006)
def _col_val(col_data: list[dict], idx: int) -> str:
    try:
        return str(col_data[idx].get("value", "")).strip()
    except (IndexError, AttributeError):
        return ""


# PLAN-EXEMPT: plan 20260713-wave2-6-tool-hardening C3 — typing modernization (UP006)
def _col_by_role(col_data: list[dict], indices: dict[str, int], role: str, fallback_indices: list[int]) -> str:
    if role in indices:
        return _col_val(col_data, indices[role])
    for idx in fallback_indices:
        val = _col_val(col_data, idx)
        if val:
            return val
    return ""


def _txn_id_from_cells(col_data: list[dict], indices: dict[str, int]) -> str | None:
    """The QBO transaction id the GL report attaches to the type (or doc number) cell."""
    for role, fallback in (("type", 1), ("doc_num", 2)):
        idx = indices.get(role, fallback)
        try:
            cell = col_data[idx]
        except IndexError:
            continue
        txn_id = cell.get("id") if isinstance(cell, dict) else None
        if txn_id:
            return str(txn_id)
    return None


def _looks_like_txn_date(value: str) -> bool:
    return _parse_date(value) is not None


# PLAN-EXEMPT: plan 20260713-wave2-6-tool-hardening C3 — typing modernization (UP007)
def _parse_optional_amount(s: str) -> float | None:
    text = str(s or "").replace(",", "").replace("$", "").strip()
    if not text or text == "-":
        return None
    if text.startswith("(") and text.endswith(")"):
        text = "-" + text[1:-1]
    try:
        return float(text)
    except ValueError:
        return None


# PLAN-EXEMPT: plan 20260713-wave2-6-tool-hardening C3 — typing modernization (UP006/UP007)
def _account_from_section(row: dict) -> "tuple[str | None, str | None]":
    header_cols = row.get("Header", {}).get("ColData", []) or []
    if not header_cols:
        return None, None
    first = header_cols[0] or {}
    return first.get("id") or first.get("value"), first.get("value")


# PLAN-EXEMPT: plan 20260713-wave2-6-tool-hardening C3 — typing modernization (UP006/UP007)
def _iter_gl_lines(
    rows: Iterable[dict],
    columns: list[dict],
    *,
    account_id: str | None = None,
    account_name: str | None = None,
    previous_balance: float | None = None,
):
    col_indices = _build_gl_column_indices(columns or [])

    for row in rows:
        row_type = row.get("type", "")
        if row_type == "Section":
            section_account_id, section_account_name = _account_from_section(row)
            yield from _iter_gl_lines(
                row.get("Rows", {}).get("Row", []) or [],
                columns,
                account_id=section_account_id or account_id,
                account_name=section_account_name or account_name,
                previous_balance=None,
            )
            continue

        col_data = row.get("ColData", []) or []
        if len(col_data) < 2:
            continue

        date_val = _col_by_role(col_data, col_indices, "date", [0])
        txn_date = _parse_date(date_val)
        txn_type = _col_by_role(col_data, col_indices, "type", [1])
        balance = _parse_optional_amount(_col_by_role(col_data, col_indices, "balance", [7]))

        if not txn_date or not _looks_like_txn_date(date_val) or txn_type.lower().startswith("total"):
            if balance is not None:
                previous_balance = balance
            continue

        amount = _parse_optional_amount(_col_by_role(col_data, col_indices, "amount", [6]))
        if (amount is None or abs(amount) <= 0.005) and balance is not None and previous_balance is not None:
            delta = round(balance - previous_balance, 2)
            if abs(delta) > 0.005:
                amount = delta

        if amount is None or abs(amount) <= 0.005:
            debit = _parse_optional_amount(_col_by_role(col_data, col_indices, "debit", [])) or 0.0
            credit = _parse_optional_amount(_col_by_role(col_data, col_indices, "credit", [])) or 0.0
            if debit or credit:
                amount = debit - credit

        yield {
            "account_id": account_id,
            "account_name": account_name,
            "txn_date": txn_date,
            "transaction_type": txn_type,
            "doc_number": _col_by_role(col_data, col_indices, "doc_num", [2]),
            "name": _col_by_role(col_data, col_indices, "name", [3]),
            "memo": _col_by_role(col_data, col_indices, "memo", [4]),
            "split": _col_by_role(col_data, col_indices, "split", [5]),
            "qbo_txn_id": _txn_id_from_cells(col_data, col_indices),
            "amount": round(float(amount or 0), 2),
            "balance": round(float(balance), 2) if balance is not None else None,
            "raw_data": {"ColData": col_data},
        }
        if balance is not None:
            previous_balance = balance


def upsert_gl_report_lines(
    db: Session,
    *,
    realm_id: str,
    # PLAN-EXEMPT: plan 20260713-wave2-6-tool-hardening C3 — typing modernization (UP006/UP007)
    report: dict,
    start_date: str,
    end_date: str,
    account_ids: Iterable[str] | None = None,
) -> int:
    start = _parse_date(start_date)
    end = _parse_date(end_date)
    if not start or not end:
        return 0

    account_id_set = {str(account_id) for account_id in account_ids or [] if account_id}
    delete_query = db.query(QBOGLLine).filter(
        QBOGLLine.realm_id == realm_id,
        QBOGLLine.txn_date >= start,
        QBOGLLine.txn_date <= end,
    )
    if account_id_set:
        delete_query = delete_query.filter(QBOGLLine.account_id.in_(account_id_set))
    delete_query.delete(synchronize_session=False)

    columns = report.get("Columns", {}).get("Column", []) or []
    rows = report.get("Rows", {}).get("Row", []) or []
    count = 0
    repeats: dict[tuple, int] = {}
    # One stamp for the whole window: coverage is proven per (account, window, stamp).
    synced_at = datetime.now(timezone.utc)
    # report_row_index = the line's enumeration position in the source GL report
    # (plan 20260701-pilot-fixes-batch-3 AC-2). QBO's register order for same-day
    # rows exists ONLY as report position, so it must be captured at parse time;
    # query_gl_lines replays it via (txn_date, report_row_index). Gaps from
    # filtered rows are fine — only the relative order matters.
    for report_row_index, parsed in enumerate(_iter_gl_lines(rows, columns)):
        if account_id_set and parsed.get("account_id") not in account_id_set:
            if len(account_id_set) != 1:
                continue
            parsed = dict(parsed)
            parsed["account_name"] = parsed.get("account_name") or parsed.get("account_id")
            parsed["account_id"] = next(iter(account_id_set))
        # Every posting is kept. Postings whose content is identical (two same-day
        # $500 deposits with no doc/name/memo) are numbered 0, 1, ... so each has its
        # own uq_qbo_gl_line_posting identity. The ordinal depends only on content,
        # so a concurrent upsert of an overlapping report still collides.
        content = (
            parsed.get("account_id"),
            parsed.get("txn_date"),
            parsed.get("transaction_type"),
            parsed.get("doc_number"),
            parsed.get("name"),
            parsed.get("memo"),
            parsed.get("amount"),
        )
        posting_ordinal = repeats.get(content, 0)
        repeats[content] = posting_ordinal + 1
        db.add(
            QBOGLLine(
                realm_id=realm_id,
                source_start_date=start,
                source_end_date=end,
                source_synced_at=synced_at,
                report_row_index=report_row_index,
                posting_ordinal=posting_ordinal,
                **parsed,
            )
        )
        count += 1
    db.commit()
    return count


# Freshness window for trusting the GL-line cache. A window synced longer ago than
# this is treated as stale, so a transaction edited or backdated INTO an already
# cached window can't be masked — the read falls through to a live GL fetch (which
# backfills). The cache is otherwise kept warm by sync_gl_lines in sync_all_data.
GL_CACHE_TTL_HOURS = 6


def _aware(value: datetime) -> datetime:
    # source_synced_at is timezone-aware; a naive value (some SQLite paths) is UTC.
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def gl_cache_window_proven(
    db: Session,
    *,
    realm_id: str,
    start_date: str,
    end_date: str,
    account_ids: Iterable[str] | None,
    max_age_hours: int = GL_CACHE_TTL_HOURS,
) -> bool:
    """True iff, for EVERY requested account, report windows synced within
    ``max_age_hours`` contiguously span ``[start_date, end_date]``.

    Each cached line records the GL report window it came from
    (``source_start_date``..``source_end_date``) and when that report was fetched
    (``source_synced_at``). Upserting a window first deletes every cached line of
    those accounts dated inside it, so an account's fresh windows are exactly the
    date ranges whose cached lines are a complete, recent copy of QBO. The old gate
    only checked the newest end date, so a month straddling the start of the synced
    window was served with its early postings missing.

    An account with no cached line cannot be proven (no activity and never cached
    look the same), and a request without an account filter is never proven:
    windows fetched for one account say nothing about the others. Both fall through
    to the live read, which is always correct.
    """
    start = _parse_date(start_date)
    end = _parse_date(end_date)
    account_id_set = {str(account_id) for account_id in account_ids or [] if account_id}
    if not start or not end or start > end or not account_id_set:
        return False
    windows = (
        db.query(
            QBOGLLine.account_id,
            QBOGLLine.source_start_date,
            QBOGLLine.source_end_date,
            func.count(QBOGLLine.id),
            func.count(QBOGLLine.source_synced_at),
            func.min(QBOGLLine.source_synced_at),
        )
        .filter(
            QBOGLLine.realm_id == realm_id,
            QBOGLLine.account_id.in_(account_id_set),
            QBOGLLine.source_start_date.isnot(None),
            QBOGLLine.source_end_date.isnot(None),
            QBOGLLine.source_start_date <= end,
            QBOGLLine.source_end_date >= start,
        )
        .group_by(QBOGLLine.account_id, QBOGLLine.source_start_date, QBOGLLine.source_end_date)
        .all()
    )
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    fresh: dict[str, list[tuple[date, date]]] = {account_id: [] for account_id in account_id_set}
    for account_id, window_start, window_end, line_count, stamped_count, synced_at in windows:
        # A window holding any unstamped line (cached under the old lossy identity)
        # proves nothing; neither does a stale one.
        if stamped_count != line_count or synced_at is None or _aware(synced_at) < cutoff:
            continue
        fresh[account_id].append((window_start, window_end))
    for spans in fresh.values():
        covered_through = start - timedelta(days=1)
        for window_start, window_end in sorted(spans):
            if window_start > covered_through + timedelta(days=1):
                break
            covered_through = max(covered_through, window_end)
        if covered_through < end:
            return False
    return True


# PLAN-EXEMPT: ratified plan 20260713-wave2-6-tool-hardening chunk C3 (marker
# RATIFICATION-2026-07-13.md on disk; active-plan.json sidecar absent in this
# dispatched session — standing precedent). Decision 6 / AC-4: keyset cursor over
# the register order (txn_date, COALESCE(report_row_index, sentinel), id).
# NULL report_row_index rows sort last within their date (the legacy nulls_last
# behavior), so the keyset coalesces NULL to a sentinel LARGER than any real
# report position — a naive tuple comparison on the raw nullable column drops or
# repeats NULL-index rows at page boundaries (the E10 trap).
_GL_NULL_ROW_INDEX_SENTINEL = 2_147_483_647  # int32 max; real report positions are tiny
_CENT = Decimal("0.01")


def _encode_gl_cursor(payload: dict) -> str:
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True, default=str)
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def _decode_gl_cursor(token: str) -> dict:
    """Decode a cursor token; a garbled/foreign token fails loud (never a silent page-1 restart)."""
    try:
        decoded = json.loads(base64.urlsafe_b64decode(token.encode("ascii")).decode("utf-8"))
        return {
            "d": date.fromisoformat(str(decoded["d"])),
            "r": int(decoded["r"]),
            "i": int(decoded["i"]),
            "b": Decimal(str(decoded["b"])),
            "tc": int(decoded["tc"]),
            "ta": Decimal(str(decoded["ta"])),
        }
    except (KeyError, TypeError, ValueError, ArithmeticError, binascii.Error) as exc:
        raise ValueError("Invalid GL cursor token — restart the read without a cursor.") from exc


def _gl_window_opening(query_factory, coalesced_index) -> Decimal:
    """Window-wide opening balance, computed ONCE per window (never re-anchored per page).

    Anchor = the LAST row in register order over the WHOLE window that carries a
    QBO-reported balance; opening = anchor_balance − Σ(amount through the anchor).
    Both legs run in SQL so no page limit truncates the anchor search."""
    anchor = (
        query_factory()
        .filter(QBOGLLine.balance.isnot(None))
        .order_by(QBOGLLine.txn_date.desc(), coalesced_index.desc(), QBOGLLine.id.desc())
        .first()
    )
    if anchor is None:
        return Decimal("0")
    anchor_index = anchor.report_row_index if anchor.report_row_index is not None else _GL_NULL_ROW_INDEX_SENTINEL
    through_anchor = or_(
        QBOGLLine.txn_date < anchor.txn_date,
        and_(QBOGLLine.txn_date == anchor.txn_date, coalesced_index < anchor_index),
        and_(QBOGLLine.txn_date == anchor.txn_date, coalesced_index == anchor_index, QBOGLLine.id <= anchor.id),
    )
    cumulative = (
        query_factory().filter(through_anchor).with_entities(func.coalesce(func.sum(QBOGLLine.amount), 0)).scalar()
    )
    return (Decimal(str(anchor.balance)) - Decimal(str(cumulative or 0))).quantize(_CENT, rounding=ROUND_HALF_UP)


def query_gl_lines_page(
    db: Session,
    *,
    realm_id: str,
    start_date: str,
    end_date: str,
    account_ids: Iterable[str] | None = None,
    limit: int | None = 500,
    cursor: str | None = None,
) -> dict:
    """One stable keyset page over the cached GL register (the 2.3 recon page shape:
    items + total + next_cursor + has_more; agent_ops.serialize_unmatched precedent).

    ``total_count``/``total_amount`` are WINDOW aggregates (COUNT + net SUM with no
    limit), computed once on the first page and threaded through the cursor token —
    the pre-fix ``total`` was the post-limit page sum, which lied about the window.
    The running balance is anchored once per window (``_gl_window_opening``) and the
    balance after the page's last row rides the token, so pages never re-anchor.
    Register order per plan 20260701-pilot-fixes-batch-3 AC-3; Decimal throughout
    (financial-modeling-correctness) — floats drift over long registers.

    The cache answers only for a window it can prove complete and fresh
    (``gl_cache_window_proven``). Otherwise the first page comes back empty, which
    every caller treats as "read QBO live", and a continuation cursor fails loud
    (the window it was cut from can no longer be trusted, so the read restarts)."""
    empty_page = {
        "transactions": [], "total_count": 0, "total_amount": 0.0,
        "limit": limit, "next_cursor": None, "has_more": False,
    }
    start = _parse_date(start_date)
    end = _parse_date(end_date)
    if not start or not end:
        return empty_page

    token = _decode_gl_cursor(cursor) if cursor else None
    if not gl_cache_window_proven(
        db, realm_id=realm_id, start_date=start_date, end_date=end_date, account_ids=account_ids,
    ):
        if token is not None:
            raise ValueError("The cached GL window is no longer complete and fresh — restart the read without a cursor.")
        return empty_page

    coalesced_index = func.coalesce(QBOGLLine.report_row_index, _GL_NULL_ROW_INDEX_SENTINEL)
    account_id_set = {str(account_id) for account_id in account_ids or [] if account_id}

    def _window_query():
        query = db.query(QBOGLLine).filter(
            QBOGLLine.realm_id == realm_id,
            QBOGLLine.txn_date >= start,
            QBOGLLine.txn_date <= end,
        )
        if account_id_set:
            query = query.filter(QBOGLLine.account_id.in_(account_id_set))
        return query

    if token is None:
        total_count, total_sum = (
            _window_query().with_entities(func.count(QBOGLLine.id), func.coalesce(func.sum(QBOGLLine.amount), 0)).one()
        )
        total_amount = Decimal(str(total_sum or 0)).quantize(_CENT, rounding=ROUND_HALF_UP)
        running = _gl_window_opening(_window_query, coalesced_index)
    else:
        total_count, total_amount, running = token["tc"], token["ta"], token["b"]

    page_query = _window_query()
    if token is not None:
        page_query = page_query.filter(
            or_(
                QBOGLLine.txn_date > token["d"],
                and_(QBOGLLine.txn_date == token["d"], coalesced_index > token["r"]),
                and_(QBOGLLine.txn_date == token["d"], coalesced_index == token["r"], QBOGLLine.id > token["i"]),
            )
        )
    page_query = page_query.order_by(QBOGLLine.txn_date.asc(), coalesced_index.asc(), QBOGLLine.id.asc())
    fetch_limit = max(1, int(limit)) if limit is not None else None
    if fetch_limit is not None:
        page_query = page_query.limit(fetch_limit + 1)  # +1 sentinel row answers has_more without a COUNT
    rows = page_query.all()
    has_more = fetch_limit is not None and len(rows) > fetch_limit
    if has_more:
        rows = rows[:fetch_limit]

    results: list[dict] = []
    for row in rows:  # SA-OK: bounded-loop over the already-fetched page rows; no query inside
        amount = Decimal(str(row.amount or 0)).quantize(_CENT, rounding=ROUND_HALF_UP)
        running = (running + amount).quantize(_CENT, rounding=ROUND_HALF_UP)
        results.append({
            "date": row.txn_date.isoformat(),
            "type": row.transaction_type or "",
            "doc_num": row.doc_number or "",
            "name": row.name or "",
            "memo": row.memo or "",
            "split": row.split or "",
            "amount": float(amount),
            "balance": float(running),
            "account_id": row.account_id,
            "account_name": row.account_name,
        })

    next_cursor = None
    if has_more and rows:
        last = rows[-1]
        next_cursor = _encode_gl_cursor({
            "d": last.txn_date.isoformat(),
            "r": last.report_row_index if last.report_row_index is not None else _GL_NULL_ROW_INDEX_SENTINEL,
            "i": last.id,
            "b": str(running),
            "tc": int(total_count or 0),
            "ta": str(total_amount),
        })
    return {
        "transactions": results,
        "total_count": int(total_count or 0),
        "total_amount": float(total_amount),
        "limit": limit,
        "next_cursor": next_cursor,
        "has_more": has_more,
    }


def query_gl_lines(
    db: Session,
    *,
    realm_id: str,
    start_date: str,
    end_date: str,
    account_ids: Iterable[str] | None = None,
    limit: int | None = 500,  # PLAN-EXEMPT: batch-4 bug 2 — drill-down passes None for the full posting set
) -> "tuple[list[dict], float]":
    """Legacy 2-tuple wrapper over ``query_gl_lines_page`` (first page, no cursor).

    The returned total is now the WINDOW net (COUNT/SUM without limit) — the E10
    fix; the drill-down callers pass ``limit=None`` where window == page, so their
    behavior is unchanged."""
    page = query_gl_lines_page(
        db, realm_id=realm_id, start_date=start_date, end_date=end_date,
        account_ids=account_ids, limit=limit,
    )
    return page["transactions"], page["total_amount"]


def format_gl_lines_for_assistant(
    # PLAN-EXEMPT: plan 20260713-wave2-6-tool-hardening C3 — typing modernization (UP006)
    transactions: list[dict],
    *,
    start_date: str,
    end_date: str,
    source: str = "Peregrine local GL cache",
    max_lines: int = 80,
) -> str:
    lines = [f"General Ledger ({start_date} to {end_date})", f"Source: {source}"]
    if not transactions:
        lines.append("No individual transactions found.")
        return "\n".join(lines)

    total = sum(float(txn.get("amount") or 0) for txn in transactions)
    lines.append(f"Transactions: {len(transactions)} | Net activity: ${total:,.2f}")
    current_account = None
    for txn in transactions[:max_lines]:
        account = txn.get("account_name") or txn.get("account_id") or "Unknown account"
        if account != current_account:
            lines.append(f"\n{account}")
            current_account = account
        memo = str(txn.get("memo") or "")[:80]
        lines.append(
            f"  {txn.get('date')} | {txn.get('type')} | {txn.get('name')} | {memo} | ${float(txn.get('amount') or 0):,.2f}"
        )
    if len(transactions) > max_lines:
        lines.append(f"... ({len(transactions) - max_lines} more transactions omitted)")
    return "\n".join(lines)

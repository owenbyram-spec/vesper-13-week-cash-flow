# PLAN-EXEMPT: ratified plan 20260702-qa-remediation W-7 — the board_pack report's
# ONE bounded, cached, ledger-metered LLM commentary pass (AC-2); a NEW module beside
# the other per-report builders (the plan's W-7 dispatch owns app/reporting/**).
"""
app/reporting/board_commentary.py
──────────────────────────────────
The board_pack report's ONE LLM commentary pass (W-7, AC-2).

Contract (all four legs are load-bearing):

1. **ONE call, ever, per build.** Structured section JSON in → per-section
   2-3 sentence commentary out, via a single ``analysis_llm.predict_json`` with
   ``allow_fallback=False`` — a provider failure can NEVER trigger a second
   provider call from this pass. Any failure (provider error, JSON parse
   failure, cap refusal) degrades to ``({}, "unavailable")`` and the report
   renders with an honest per-section placeholder.

2. **Ledger-metered.** The call carries ``task_type="board_report"`` through
   the W-1 metering seam (``LaneLLMService._record_usage`` →
   ``record_llm_usage``), so every board-pack commentary spend is a visible
   ``LLMUsageEvent`` row in the admin rollup.

3. **Hard token cap (deterministic, pre-call).**
   ``settings.board_report_commentary_token_cap`` (~8k default) bounds the
   ESTIMATED prompt tokens (the standard ~4-chars/token heuristic over
   system + user prompt) BEFORE the provider is invoked — at/over the cap the
   call is refused outright (zero provider spend), never truncated silently.

4. **Cached per (realm, period).** The generated commentary is stored as a
   ``ReportSection`` row (``company_id`` = the realm, ``period``,
   ``section_type == "board_pack_commentary"``) — an EXISTING tenant-scoped
   table (realm-keyed in tenant_data_scope), so NO new migration. A cache hit
   makes regeneration free: the second build performs ZERO LLM calls. The
   insights pipeline's per-(company, period) ReportSection sweep evicts the
   row on a data re-sync, which is semantically correct — fresh data earns one
   fresh (bounded, metered) pass. Failures are NOT cached (the next build may
   retry once).

TENANT-SAFETY: the caller (routers/reporting.py → build_board_pack) gates
tenant access via ``check_tenant_access`` BEFORE any builder runs; the cache
read/write below uses the SAME request session (already realm-pinned), keyed
by the trusted ``company_id``.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any  # CRAFT-OK: Any = dynamic LLM/JSON payload boundary

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

Payload = dict[str, Any]  # CRAFT-OK: dynamic report/LLM JSON payload boundary

# The ReportSection.section_type key under which the commentary caches.
BOARD_COMMENTARY_SECTION_TYPE = "board_pack_commentary"

# The closed set of board-pack commentary sections: (payload key, display label).
# The LLM must return exactly these keys; anything else is dropped on validation.
BOARD_COMMENTARY_SECTIONS: tuple[tuple[str, str], ...] = (
    ("covenant_compliance", "Covenant Compliance"),
    ("ar_ap_aging", "AR / AP Aging"),
    ("budget_vs_actuals", "Budget vs. Actuals"),
    ("cash_flow_forecast", "13-Week Cash Flow Forecast"),
)
BOARD_COMMENTARY_KEYS: tuple[str, ...] = tuple(k for k, _ in BOARD_COMMENTARY_SECTIONS)

# Defensive per-section length clamp on MODEL OUTPUT (display sanity, not a
# token bound — the token bound is the pre-call cap).
_MAX_SECTION_CHARS = 1500

_SYSTEM_PROMPT = (
    "You are a senior CFO analyst writing concise board-pack section commentary. "
    "Respond ONLY with a JSON object."
)


def _estimate_tokens(*texts: str) -> int:
    """Deterministic prompt-size estimate: the standard ~4 characters/token
    heuristic. Used ONLY for the pre-call hard cap (a conservative, reproducible
    bound) — never for billing (the ledger records the provider's real usage)."""
    return sum(len(t) for t in texts) // 4


def _build_prompt(section_context: Payload) -> str:
    import json

    labels = "\n".join(f'- "{key}": {label}' for key, label in BOARD_COMMENTARY_SECTIONS)
    return (
        "Here is the structured data for each section of a monthly board pack:\n"
        f"{json.dumps(section_context, indent=1, default=str)}\n\n"
        "Write commentary for these sections:\n"
        f"{labels}\n\n"
        "Return a JSON object whose keys are EXACTLY those section keys and whose "
        "values are each a 2-3 sentence professional commentary on that section's "
        "data. Use specific figures from the data. If a section's data is empty or "
        "unavailable, say so plainly in one sentence. Do NOT invent numbers that "
        "are not in the data. No markdown, no headers — plain prose sentences only."
    )


def _validate_commentary(raw: Any) -> dict[str, str]:  # CRAFT-OK: Any = unvalidated LLM JSON boundary
    """Clamp the model's JSON to the closed section-key set: known keys with
    non-empty string values only, length-clamped. Unknown keys are dropped."""
    out: dict[str, str] = {}
    if not isinstance(raw, dict):
        return out
    for key in BOARD_COMMENTARY_KEYS:
        value = raw.get(key)  # SA-OK: bounded-loop 4 in-memory dict.get over the fixed section-key tuple, not a DB query
        if isinstance(value, str) and value.strip():
            out[key] = value.strip()[:_MAX_SECTION_CHARS]
    return out


def _read_cache(db: Session, company_id: str, period: str) -> dict[str, str] | None:
    """The cached commentary for (realm, period), or None. Read through the
    caller's (realm-pinned) session; any read error is a cache MISS, never a
    build failure."""
    from ..models import ReportSection

    try:
        row = (
            db.query(ReportSection)
            .filter(
                ReportSection.company_id == company_id,
                ReportSection.period == period,
                ReportSection.section_type == BOARD_COMMENTARY_SECTION_TYPE,
            )
            .first()
        )
    except Exception as exc:
        logger.warning("[board_commentary] cache read failed (treating as miss): %s", exc)
        return None
    if row is None:
        return None
    content = row.content if isinstance(row.content, dict) else {}
    commentary = _validate_commentary(content.get("commentary"))
    return commentary or None


def _write_cache(db: Session, company_id: str, period: str, commentary: dict[str, str]) -> None:
    """Best-effort upsert of the commentary cache row. The write must NEVER fail
    the report build — on any error the commentary is still returned uncached.
    Uses a SAVEPOINT (begin_nested) so a conflicting concurrent insert under
    FORCE-RLS rolls back only the cache write, then commits (the request path
    that serves downloads never commits otherwise, and at this point in the
    build no other writes are pending on the session)."""
    from ..models import ReportSection

    try:
        with db.begin_nested():
            db.query(ReportSection).filter(
                ReportSection.company_id == company_id,
                ReportSection.period == period,
                ReportSection.section_type == BOARD_COMMENTARY_SECTION_TYPE,
            ).delete()
            # LAYER-OK: cache-row persistence beside the reporting builders (the
            # module-local seam the report engine owns, mirroring the legacy router).
            db.add(ReportSection(
                company_id=company_id,
                period=period,
                section_type=BOARD_COMMENTARY_SECTION_TYPE,
                content={
                    "commentary": commentary,
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                },
                order_index=999,
            ))
        db.commit()
    except Exception as exc:
        logger.warning("[board_commentary] cache write failed (commentary still served): %s", exc)
        try:
            db.rollback()
        except Exception as rb_exc:  # CRAFT-OK: best-effort rollback of an already-failed cache write; logged, never masks the build
            logger.warning("[board_commentary] rollback after failed cache write also failed: %s", rb_exc)


def get_board_commentary(
    db: Session, company_id: str, period: str, section_context: Payload
) -> tuple[dict[str, str], str]:
    """The board pack's commentary map + an honest status.

    Returns ``(commentary, status)`` where commentary maps section key →
    2-3 sentence text (possibly ``{}``) and status is one of:

    - ``"cached"``      — served from the (realm, period) cache; ZERO LLM calls.
    - ``"generated"``   — ONE metered LLM call succeeded; result cached.
    - ``"no_data"``     — every section was empty; no call made (nothing to say).
    - ``"cap_exceeded"``— the deterministic pre-call token estimate hit the hard
                          cap; NO call made.
    - ``"unavailable"`` — the ONE call failed / parsed invalid; NO second call.
    """
    from ..config import settings

    cached = _read_cache(db, company_id, period)
    if cached is not None:
        return cached, "cached"

    # Nothing to comment on → refuse the spend honestly (deterministic guard).
    if not any(section_context.get(k) for k in BOARD_COMMENTARY_KEYS):
        return {}, "no_data"

    user_prompt = _build_prompt(section_context)
    cap = int(settings.board_report_commentary_token_cap)
    estimated = _estimate_tokens(_SYSTEM_PROMPT, user_prompt)
    if estimated >= cap:
        logger.warning(
            "[board_commentary] pre-call token estimate %s >= cap %s for realm=%s "
            "period=%s — commentary refused (report renders with placeholder)",
            estimated, cap, company_id, period,
        )
        return {}, "cap_exceeded"

    try:
        from ..services.llm import analysis_llm

        raw = analysis_llm.predict_json(
            system_prompt=_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            temperature=0.3,
            # AC-2: ONE call — a provider failure must never cascade into a
            # second provider call from this pass.
            allow_fallback=False,
            # W-1 ledger dimension: every board-pack commentary spend is visible.
            task_type="board_report",
        )
    except Exception as exc:
        logger.warning("[board_commentary] commentary call failed: %s", exc)
        return {}, "unavailable"

    commentary = _validate_commentary(raw)
    if not commentary:
        return {}, "unavailable"

    _write_cache(db, company_id, period, commentary)
    return commentary, "generated"

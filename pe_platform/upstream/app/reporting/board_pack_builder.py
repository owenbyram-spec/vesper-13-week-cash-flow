# PLAN-EXEMPT: ratified plan 20260702-qa-remediation W-7 — the board_pack payload
# builder (AC-1); a NEW module beside the other per-report builders (the plan's W-7
# dispatch owns app/reporting/**), kept separate per the 500-line file discipline.
"""
app/reporting/board_pack_builder.py
────────────────────────────────────
Payload builder for the ``board_pack`` report (W-7, F-15 / item #15).

**AC-1 — 100% deterministic assembly from EXISTING builders.** Every section's
figures come verbatim from the engine that already owns them:

- Covenant monthly grid   → ``build_covenant_monthly`` (compute_monthly_series)
- AR/AP per-entity aging  → ``build_ar_ap_aging`` (ar_aging()/ap_aging(), the
                            W-4 per-entity grid renders it)
- Budget vs. actuals      → ``build_budget_map`` (the month-end gather's exact
                            BudgetLine query) + the month-end P&L detail fetch +
                            ``_compute_bva_variances`` (the month-end BVA math)
- CFF weekly summary      → ``build_cashflow_forecast`` (the /cashflow-forecast
                            weekly service, runway = min-selection)

This builder re-derives NO financial number (AP-DR-06) — it calls the owners
and merges their payload keys so the EXISTING section renderers display them.
A failed sub-build records an honest unavailable state (safe_error_detail +
data_quality) and the pack still renders — never a fabricated section.

**AC-2 — ONE bounded LLM commentary pass** lives in ``board_commentary``:
compact structured section JSON in → per-section 2-3 sentence commentary out,
task_type="board_report" through the W-1 ledger, hard pre-call token cap,
cached per (realm, period). The two reused builders that normally make their
own per-report narrative call are invoked with ``include_commentary=False`` so
the assembly performs ZERO LLM calls outside that one pass.

TENANT-SAFETY PRECONDITION (AP-DR-07): the caller (routers/reporting.py) gates
tenant access via ``check_tenant_access`` BEFORE invoking this builder; every
sub-build is keyed by that trusted ``company_id`` (realm-scoped QBO client +
tenant-filtered DB queries).
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any  # CRAFT-OK: Any = dynamic provider-derived QBO/report payload boundary

from sqlalchemy.orm import Session

from ..services.errors import safe_error_detail
from .board_commentary import BOARD_COMMENTARY_KEYS, get_board_commentary

logger = logging.getLogger(__name__)

Payload = dict[str, Any]  # CRAFT-OK: dynamic provider-derived report payload boundary

# Honest empty shapes for a failed sub-build (the section renderers' empty states).
_EMPTY_AGING_AR: Payload = {"as_of": None, "buckets": {}, "total_ar": None, "invoices": []}
_EMPTY_AGING_AP: Payload = {"as_of": None, "buckets": {}, "total_ap": None, "bills": []}


def _build_bva_block(db: Session, company_id: str, period: str) -> Payload:
    """Budget-vs-actuals from the EXISTING month-end pieces: ``build_budget_map``
    (the exact BudgetLine query) + the month-end P&L detail fetch + the month-end
    ``_compute_bva_variances``. No budget configured → an honest empty (no QBO
    fetch spent); DISPLAY-ONLY otherwise."""
    # Function-level imports break the report_builder ↔ board_pack_builder cycle
    # (report_builder registers this module's builder at import time).
    from .data_gatherer import (
        _fetch_raw_report,
        build_budget_map,
        parse_qbo_report_rows,
        parse_report_period,
    )
    from .report_builder import _compute_bva_variances

    # A malformed period raises (recorded as board_bva_unavailable by the caller):
    # budget and actuals must both be for the month asked for, never today's month
    # against the last completed one.
    year, month = parse_report_period(period)

    budget_map = build_budget_map(db, company_id, year, month)
    if not budget_map:
        return {"bva_top_variances": [], "budget_available": False}

    from ..analysis.ratios import resolve_period
    from ..qbo_client import get_qbo_client_from_db

    client = get_qbo_client_from_db(db, company_id)
    resolved = resolve_period(year, month, db=db, realm_id=company_id)
    raw_pnl = _fetch_raw_report(client, "ProfitAndLoss", {
        "start_date": resolved["start_date"],
        "end_date": resolved["end_date"],
        "summarize_column_by": "Total",
        "accounting_method": "Accrual",
    })
    pnl_detail = parse_qbo_report_rows(raw_pnl) if raw_pnl else []
    return {
        "bva_top_variances": _compute_bva_variances(pnl_detail, budget_map),
        "budget_available": True,
    }


def _commentary_context(payload: Payload) -> Payload:
    """The compact, bounded, structured section JSON the ONE commentary call
    reads (AC-2). Every figure is lifted verbatim from the already-assembled
    deterministic payload; lists are top-N bounded so the prompt stays small.
    An empty section contributes an empty value (the honest no-data signal)."""
    covenants: list[Payload] = []
    for entry in (payload.get("monthly_covenants") or [])[:10]:
        series = entry.get("series") or {}  # SA-OK: bounded-loop in-memory covenant-entry dict.get, not a DB query
        covenants.append({
            "name": entry.get("name"),
            "loan": entry.get("loan_name"),
            "metric": entry.get("metric_type"),
            "threshold": entry.get("threshold"),
            "breach_direction": entry.get("breach_direction"),
            "available": entry.get("available"),
            "ttm_value": series.get("ttm_ratio"),
            "reason": entry.get("reason"),
        })

    def _top_entities(entities: list, name_key: str) -> list[Payload]:
        rows = sorted(
            entities or [],
            key=lambda e: abs(float(e.get("balance") or 0)),
            reverse=True,
        )[:5]
        return [
            {"name": e.get(name_key), "balance": e.get("balance")}
            for e in rows
        ]

    ar = payload.get("ar_aging") or {}
    ap = payload.get("ap_aging") or {}
    aging: Payload = {}
    if (ar.get("total_ar") is not None) or (ap.get("total_ap") is not None):
        aging = {
            "total_ar": ar.get("total_ar"),
            "ar_buckets": ar.get("buckets") or {},
            "top_customers": _top_entities(ar.get("invoices") or [], "customer"),
            "total_ap": ap.get("total_ap"),
            "ap_buckets": ap.get("buckets") or {},
            "top_vendors": _top_entities(ap.get("bills") or [], "vendor"),
        }

    variances = payload.get("bva_top_variances") or []
    bva: Payload = {}
    if variances:
        def _slim(rows: list[Payload]) -> list[Payload]:
            return [
                {
                    "account": v.get("account"),
                    "actual": v.get("actual"),
                    "budget": v.get("budget"),
                    "variance": v.get("variance_dollar"),
                }
                for v in rows[:5]
            ]
        bva = {
            "favorable": _slim([v for v in variances if v.get("direction") == "favorable"]),
            "unfavorable": _slim([v for v in variances if v.get("direction") == "unfavorable"]),
        }

    fc = payload.get("forecast") or {}
    runway = payload.get("runway") or {}
    cff: Payload = {}
    if fc.get("week_headers"):
        ending = fc.get("ending_balances") or []
        cff = {
            "opening_balance": fc.get("opening_balance"),
            "weeks": len(fc.get("week_headers") or []),
            "final_ending_balance": ending[-1] if ending else None,
            "lowest_balance": runway.get("lowest_balance"),
            "lowest_balance_date": runway.get("lowest_balance_date"),
            "weeks_to_negative": runway.get("weeks_to_negative"),
        }

    # Keys MUST match BOARD_COMMENTARY_KEYS (validated at import below).
    return {
        "covenant_compliance": covenants,
        "ar_ap_aging": aging,
        "budget_vs_actuals": bva,
        "cash_flow_forecast": cff,
    }


# Fail-fast import guard: the context keys and the commentary contract must agree.
assert set(_commentary_context({}).keys()) == set(BOARD_COMMENTARY_KEYS)


def build_board_pack(db: Session, company_id: str, period: str) -> Payload:
    """Assemble the board_pack payload: four deterministic sections merged from
    their owning builders + the ONE bounded/cached/metered commentary pass."""
    from .covenant_monthly_builder import build_covenant_monthly
    from .named_report_builders import build_ar_ap_aging, build_cashflow_forecast

    errors: list[Payload] = []

    def _sub(fn, *, error_code: str, user_message: str, fallback: Payload) -> Payload:
        """Run one sub-build; a failure records an honest partial-data error and
        returns the section's empty shape — the pack still renders (AC-3)."""
        try:
            return fn()
        except Exception as exc:
            errors.append(safe_error_detail(
                error_code=error_code,
                user_message=user_message,
                exc=exc,
                retryable=True,
                log_context=f"board_pack_{error_code}",
            ))
            return fallback

    cov = _sub(
        lambda: build_covenant_monthly(db, company_id, period),
        error_code="board_covenant_unavailable",
        user_message="Covenant compliance data could not be loaded for the board pack.",
        fallback={"monthly_covenants": [], "covenant_window": None, "metadata": {}},
    )
    aging = _sub(
        lambda: build_ar_ap_aging(db, company_id, period, include_commentary=False),
        error_code="board_aging_unavailable",
        user_message="AR/AP aging data could not be loaded for the board pack.",
        fallback={"ar_aging": dict(_EMPTY_AGING_AR), "ap_aging": dict(_EMPTY_AGING_AP),
                  "metadata": {}, "data_quality": {}},
    )
    bva = _sub(
        lambda: _build_bva_block(db, company_id, period),
        error_code="board_bva_unavailable",
        user_message="Budget-vs-actuals data could not be loaded for the board pack.",
        fallback={"bva_top_variances": [], "budget_available": False},
    )
    cff = _sub(
        lambda: build_cashflow_forecast(db, company_id, period, include_commentary=False),
        error_code="board_cff_unavailable",
        user_message="Cash-flow forecast data could not be loaded for the board pack.",
        fallback={"forecast": {}, "runway": {}, "metadata": {}, "errors": [],
                  "data_quality": {}},
    )

    # Merge sub-build partial-data signals (each sub-payload's own data_quality
    # already lists its internal degradations, e.g. one aging side failing).
    for sub in (aging, cff):
        errors.extend(sub.get("errors") or [])
    unavailable: list[str] = []
    for err in errors:
        if isinstance(err, dict) and err.get("error_code"):
            unavailable.append(err["error_code"])
    for sub in (aging, cff):
        for code in (sub.get("data_quality") or {}).get("unavailable_sections", []) or []:
            if code not in unavailable:
                unavailable.append(code)  # SA-OK: bounded-loop in-memory data_quality list merge, not a DB query

    # Company name: reuse whichever sub-builder already fetched it (no extra call).
    company_name = ""
    for sub in (cov, aging, cff):
        company_name = (sub.get("metadata") or {}).get("company_name") or ""  # SA-OK: bounded-loop 3 in-memory metadata dict.get, not a DB query
        if company_name:
            break

    payload: Payload = {
        "metadata": {
            "company_id": company_id,
            "company_name": company_name,
            "period": period,
            "period_label": period,
            "title": f"Board Pack — {period}",
            "generated_at": datetime.utcnow().strftime("%B %d, %Y at %I:%M %p UTC"),
        },
        # Covenant monthly grid (verbatim from build_covenant_monthly).
        "monthly_covenants": cov.get("monthly_covenants", []),
        "covenant_window": cov.get("covenant_window"),
        # AR/AP per-entity aging (verbatim from build_ar_ap_aging).
        "ar_aging": aging.get("ar_aging", dict(_EMPTY_AGING_AR)),
        "ap_aging": aging.get("ap_aging", dict(_EMPTY_AGING_AP)),
        # Budget vs actuals (the month-end BVA math over the shared budget map).
        "bva_top_variances": bva.get("bva_top_variances", []),
        "budget_available": bool(bva.get("budget_available")),
        # CFF weekly grid + runway (verbatim from build_cashflow_forecast).
        "forecast": cff.get("forecast", {}),
        "runway": cff.get("runway", {}),
        "errors": errors,
        "data_quality": {
            "is_partial": bool(unavailable),
            "unavailable_sections": unavailable,
        },
    }

    # AC-2 — the ONE bounded, cached, ledger-metered commentary pass. Rendering
    # NEVER depends on it: any non-ok status renders the honest placeholder.
    commentary, status = get_board_commentary(
        db, company_id, period, _commentary_context(payload))
    payload["board_commentary"] = commentary
    payload["board_commentary_status"] = status
    return payload

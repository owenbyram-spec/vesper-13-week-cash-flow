# PLAN-EXEMPT: AC-3/AC-4 named-report payload builders under ratified plan 20260629-custom-reporting; the builders live beside report_builder.py (which IS in surface_scope) but in their own module to stay under the 500-line cap — surface_scope enumerated report_builder.py, not this sibling.
"""
app/reporting/named_report_builders.py
────────────────────────────────────────
Payload builders for the AC-3 / AC-4 reuse reports (standard_financials, ar_ap_aging).

Each builder has the SAME signature as the month_end builder
(`(db, company_id, period) -> payload dict`) so it slots straight into
`report_builder.PAYLOAD_BUILDERS` and is dispatched by `build_report_payload`.

DISPLAY-ONLY (AP-DR-06): a builder fetches its source data and shapes it for the
section renderers; it never re-figures a financial number. The aging builder
returns `ar_aging()`/`ap_aging()` output verbatim so the buckets tie to
`ar_aging.py` / `ap_aging.py`. (The standard-financials builder lives in
`standard_financials_builder.py` since W-4 fix #7.)

TENANT-SAFETY PRECONDITION (AP-DR-07): the caller (routers/reporting.py) gates
tenant access via `check_tenant_access` BEFORE invoking a builder, and the QBO
client is realm-scoped via `get_qbo_client_from_db(db, company_id)` — the same gate
the `/api/reports/*` endpoints use.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any  # CRAFT-OK: Any = dynamic provider-derived QBO report/CompanyInfo payload boundary

from sqlalchemy.orm import Session

from ..analysis.ap_aging import ap_aging
from ..analysis.ar_aging import ar_aging
from ..cashflow.export_snapshots import (
    ExportSnapshotNotReadyError,
    load_forecast_export,
)
from ..cashflow.schemas import ForecastGridExport, ForecastGridRow
from ..qbo_client import get_qbo_client_from_db
from ..services.tenant_companies import organization_for_company
from .analysis_notes import generate_analysis_notes

# A report payload is dynamic provider-derived QBO/DB JSON of varying shape.
Payload = dict[str, Any]  # CRAFT-OK: dynamic provider-derived QBO report payload boundary

# NOTE (W-4 fix #7): build_standard_financials moved to standard_financials_builder.py
# (this module sits near the 500-line cap; the monthly-columns rework needed room).
# PLAN-EXEMPT: ratified plan 20260702-qa-remediation W-4


def _metadata(company_id: str, period: str, company_name: str, title: str) -> Payload:
    return {
        "company_id": company_id,
        "company_name": company_name,
        "period": period,
        "period_label": period,
        "title": title,
        "generated_at": datetime.utcnow().strftime("%B %d, %Y at %I:%M %p UTC"),
    }


def _company_name(client: Any) -> str:  # CRAFT-OK: Any = opaque QBOClient boundary
    """Best-effort company name from QBO CompanyInfo; blank on failure (display-only,
    never blocks the report)."""
    try:
        info = client.get_company_info()
        return info.get("CompanyInfo", {}).get("CompanyName", "") or ""
    except Exception:
        return ""


def _commentary(period: str, context: Payload) -> str:
    """AC-7 — generate the AI commentary narrative for a named report from a context
    dict, REUSING the existing `generate_analysis_notes` lane (which has a template
    fallback, so it never hard-fails). NEVER a hard dependency of rendering: any
    exception degrades to an empty string and the optional commentary section
    self-skips (the report still renders)."""
    try:
        ctx = {"period": period, **context}
        return generate_analysis_notes(ctx) or ""
    except Exception:
        return ""


def build_ar_ap_aging(
    db: Session, company_id: str, period: str, *, include_commentary: bool = True
) -> Payload:
    """AC-4 — assemble the standalone AR/AP aging payload from `ar_aging()` /
    `ap_aging()` (both force accrual basis). The buckets (0-30/31-60/61-90/90+) +
    totals + entity lists are returned VERBATIM so the rendered figures tie to
    ar_aging.py / ap_aging.py. A fetch failure for either side is recorded in
    data_quality so the lifted aging section shows 'temporarily unavailable' rather
    than a fabricated all-zero (it never invents a tie-out — refuse over fabricate).

    ``include_commentary=False`` (W-7 board pack) skips the per-report LLM
    narrative so the board_pack assembly stays 100% deterministic — its ONE
    bounded commentary pass happens at the board-pack layer, never per section.
    # PLAN-EXEMPT: ratified plan 20260702-qa-remediation W-7"""
    client = get_qbo_client_from_db(db, company_id)

    unavailable: list[str] = []
    try:
        ar_data = ar_aging(client)
    except Exception:
        ar_data = {"as_of": None, "buckets": {}, "total_ar": None, "invoices": []}
        unavailable.append("report_ar_aging_unavailable")
    try:
        ap_data = ap_aging(client)
    except Exception:
        ap_data = {"as_of": None, "buckets": {}, "total_ap": None, "bills": []}
        unavailable.append("report_ap_aging_unavailable")

    # AC-7 — commentary context from the AR total (display-only). The fallback
    # narrative references the AR position + liquidity framing.
    # PLAN-EXEMPT: ratified plan 20260702-qa-remediation W-7 — skipped for the
    # board pack (its ONE bounded commentary pass lives at the board-pack layer).
    notes = _commentary(period, {"bs": {"ar": ar_data.get("total_ar") or 0}}) if include_commentary else ""

    return {
        "metadata": _metadata(
            company_id, period, _company_name(client),
            f"AR / AP Aging — {period}",
        ),
        "ar_aging": ar_data,
        "ap_aging": ap_data,
        "analysis_notes": notes,  # AC-7 (optional section self-skips when empty)
        "errors": [],
        "data_quality": {
            "is_partial": bool(unavailable),
            "unavailable_sections": unavailable,
        },
    }


def build_covenant(db: Session, company_id: str, period: str) -> Payload:
    """AC-5 — the lender covenant payload IS the ``/covenants/status`` payload for the requested
    month (``routers.covenants._covenant_status_payload``): the same rows, current values,
    breakdowns and PASS/WARN/BREACH verdicts — the two-lens TTM engine for DSCR / Debt-EBITDA,
    the covenant's mapped debt accounts, the company-level EBITDA add-back fallback and each
    covenant's own WARN cushion. A fresh stored snapshot is served; otherwise the covenant is
    computed live and the snapshot stored, so a download never shows a "computing" placeholder.
    DISPLAY-ONLY (AP-DR-06): this builder re-derives NO covenant math, so the rendered figures tie
    to /covenants/status for the same month.

    ``period`` (``YYYY-MM``) selects the closed month; a current/future/malformed period falls back
    to the last closed month, and the title names the month actually evaluated."""
    from ..routers.covenants import _covenant_status_payload, _period_to_as_of

    status = _covenant_status_payload(
        db, company_id, _period_to_as_of(period), live_on_miss=True,
    )
    evaluated = str(status["as_of"])
    covenants: list[Payload] = status["covenants"]

    # AC-7 — commentary context (period only; the covenant figures live in the
    # covenant section, the narrative gives a CFO-style liquidity/risk framing).
    notes = _commentary(evaluated, {})

    metadata = _metadata(
        company_id, period, _company_name(get_qbo_client_from_db(db, company_id)),
        f"Loan Covenant Compliance — {evaluated}",
    )
    metadata["period_label"] = evaluated
    return {
        "metadata": metadata,
        "covenant_period": status["period"],
        "overall_status": status["overall_status"],
        "covenants": covenants,
        "analysis_notes": notes,  # AC-7 (optional section self-skips when empty)
        "errors": [],
        "data_quality": {
            "is_partial": any(row.get("computation_state") != "POPULATED" for row in covenants),
            "unavailable_sections": [],
        },
    }


def _grid_months(period: str, count: int = 12) -> list[str]:
    """The trailing ``count`` ``YYYY-MM`` periods ending at ``period`` (inclusive),
    oldest→newest — the column axis of the compliance grid."""
    try:
        year, month = (int(p) for p in period.split("-"))
    except (ValueError, AttributeError):
        from datetime import date
        today = date.today()
        year, month = today.year, today.month
    months: list[str] = []
    for _ in range(count):
        months.append(f"{year}-{month:02d}")
        month -= 1
        if month == 0:
            month = 12
            year -= 1
    return list(reversed(months))


def build_covenant_compliance(db: Session, company_id: str, period: str) -> Payload:
    """AC-2 — assemble the MONTHLY covenant compliance grid from the STORED
    ``CovenantRecord`` snapshots (the rows the Wave-3 snapshot task writes each closed
    month). DISPLAY-ONLY (AP-DR-06): every cell is a stored ``status`` / ``actual_value``
    read verbatim from ``covenant_records`` — this builder re-figures NO covenant math,
    so the grid ties exactly to what the snapshot task recorded.

    Shape: a per-covenant row × per-month column grid of PASS/WARN/BREACH (the months
    are the trailing 12 ``YYYY-MM`` ending at ``period``). A covenant with no snapshot
    for a month shows ``—`` (no fabricated verdict)."""
    from ..models import CovenantRecord, DebtCovenant

    months = _grid_months(period, 12)
    month_set = set(months)

    # The covenants to show as rows = the company's covenants (the live entity), tenant-scoped.
    covenants = (
        db.query(DebtCovenant)
        .filter(DebtCovenant.company_id == company_id)
        .order_by(DebtCovenant.loan_name, DebtCovenant.name)
        .all()
    )

    # The stored snapshots over the window, tenant-scoped, fetched in ONE query and
    # indexed by (covenant_id, period) — NO per-iteration DB access below.
    records = (
        db.query(CovenantRecord)
        .filter(
            CovenantRecord.company_id == company_id,
            CovenantRecord.period.in_(months),
        )
        .all()
    )
    by_key: dict[tuple[int, str], CovenantRecord] = {
        (r.covenant_id, r.period): r for r in records if r.period in month_set
    }

    rows: list[Payload] = []
    withheld = False
    for cov in covenants:
        cells: list[Payload] = []
        for m in months:
            # In-memory dict lookup over the single pre-fetched `records` query above
            # (NOT a DB query) — membership-test then index, no per-iteration round-trip.
            rec = by_key[(cov.id, m)] if (cov.id, m) in by_key else None
            if rec is not None and _legacy_negative_leverage(rec):
                withheld = True
                rec = None  # its stored verdict cannot be vouched for: shown as "—"
            cells.append({
                "period": m,
                "status": rec.status if rec else None,
                "value": rec.actual_value if rec else None,
            })
        # Skip a covenant that has NO stored snapshot in the whole window (nothing to show).
        if not any(c["status"] for c in cells):
            continue
        rows.append({
            "covenant_id": cov.id,
            "loan_name": cov.loan_name,
            "name": cov.name,
            "metric_type": cov.metric_type,
            "threshold": float(cov.threshold),
            "breach_direction": cov.breach_direction,
            "cells": cells,
        })

    try:
        company_name = _company_name(get_qbo_client_from_db(db, company_id))
    except Exception:
        company_name = ""

    notes = _commentary(period, {})
    return {
        "metadata": _metadata(
            company_id, period, company_name,
            f"Covenant Compliance History — {period}",
        ),
        "grid_months": months,
        "grid_rows": rows,
        "analysis_notes": notes,  # AC-7 (optional section self-skips when empty)
        "errors": [],
        "data_quality": {"is_partial": withheld, "unavailable_sections": []},
    }


def _legacy_negative_leverage(rec: object) -> bool:
    """A leverage-lens (Debt/EBITDA) snapshot written before not-meaningful ratios were recorded,
    holding a negative ratio: the old engine divided debt by a negative EBITDA and judged the
    negative multiple PASS. Its verdict is withheld until the period is recomputed (the
    /covenants/status as-of view rewrites it); a current row carries ``ttm.not_meaningful``."""
    ctx = getattr(rec, "calculation_context", None)
    if not isinstance(ctx, dict) or ctx.get("lens") != "leverage":
        return False
    ttm = ctx.get("ttm")
    if isinstance(ttm, dict) and "not_meaningful" in ttm:
        return False
    value = getattr(rec, "actual_value", None)
    return value is not None and value < 0


def _row(export: ForecastGridExport, kind: str) -> ForecastGridRow:
    return next(row for row in export.rows if row.row_kind == kind)


def _entity_compatibility(row: ForecastGridRow) -> Payload:
    return {
        "name": row.label,
        "entity_key": row.entity_key,
        "effective_day": row.effective_day,
        "effective_avg": row.effective_avg,
        "amounts": list(row.amounts),
    }


def cashflow_report_payload_from_export(
    export: ForecastGridExport,
    *,
    include_commentary: bool = True,
) -> Payload:
    """Return the one generic/direct renderer payload for a canonical export.

    The flattened immutable envelope travels under ``forecast.export``.  Legacy
    aliases remain alongside it for board-pack commentary and existing internal
    consumers; every alias is copied from a DTO row and no amount is recalculated.
    Standalone notes are deterministic DTO facts.  The board pack passes
    ``include_commentary=False`` and retains its one bounded LLM pass.
    """
    beginning = _row(export, "beginning_bank_balance")
    other_in = _row(export, "other_cash_in")
    total_in = _row(export, "total_cash_in")
    other_out = _row(export, "non_routine_expenses")
    total_out = _row(export, "total_cash_out")
    ending = _row(export, "ending_bank_balance")
    week_headers = [week.week_ending.isoformat() for week in export.weeks]

    liquidity = export.liquidity
    lowest_balance = liquidity.min_cash_balance if liquidity is not None else None
    lowest_balance_date = (
        liquidity.min_cash_week_ending.isoformat()
        if liquidity is not None and liquidity.min_cash_week_ending is not None
        else None
    )
    lowest_balance_week = (
        next(
            (
                ordinal
                for ordinal, week in enumerate(export.weeks[export.lookback :], start=1)
                if week.week_ending == liquidity.min_cash_week_ending
            ),
            None,
        )
        if liquidity is not None and liquidity.min_cash_week_ending is not None
        else None
    )
    weeks_to_negative = (
        liquidity.runway_weeks
        if liquidity is not None and liquidity.runway_status == "breaches_within_horizon"
        else None
    )

    period = export.as_of_week_ending.isoformat()
    forecast = {
        "export": export,
        "week_headers": week_headers,
        "week_statuses": [
            {
                "week_ending": week.week_ending.isoformat(),
                "status": week.status,
                "is_lookback": week.is_lookback,
            }
            for week in export.weeks
        ],
        "lookback": export.lookback,
        "customers": [
            _entity_compatibility(row) for row in export.rows if row.row_kind == "customer"
        ],
        "vendors": [
            _entity_compatibility(row) for row in export.rows if row.row_kind == "vendor"
        ],
        "other_cash_in": list(other_in.amounts),
        "total_cash_in": list(total_in.amounts),
        "other_cash_out": list(other_out.amounts),
        "total_cash_out": list(total_out.amounts),
        "beginning_balances": list(beginning.amounts),
        "ending_balances": list(ending.amounts),
        "opening_balance": export.bank_balance,
        "opening_balance_as_of": (
            export.bank_balance_as_of.isoformat() if export.bank_balance_as_of else None
        ),
    }
    notes = "\n\n".join(note.text for note in export.notes) if include_commentary else ""
    return {
        "metadata": {
            "company_id": export.company_id,
            "company_name": export.company_display_name,
            "period": period,
            "period_label": period,
            "title": f"13-Week Cash Flow Forecast — {period}",
            "generated_at": export.captured_at.strftime("%B %d, %Y at %I:%M %p UTC"),
        },
        "analysis_notes": notes,
        "forecast": forecast,
        "runway": {
            "lowest_balance": lowest_balance,
            "lowest_balance_week": lowest_balance_week,
            "lowest_balance_date": lowest_balance_date,
            "weeks_to_negative": weeks_to_negative,
        },
        "errors": [error.model_dump(mode="python") for error in export.errors],
        "data_quality": export.data_quality.model_dump(mode="python"),
    }


def build_cashflow_forecast(
    db: Session, company_id: str, period: str, *, include_commentary: bool = True
) -> Payload:
    """Build once, load that build's exact immutable export, and shape it once."""
    from ..routers.cashflow_forecast import _build_weekly_forecast_payload

    finalized = _build_weekly_forecast_payload(
        company_id=company_id,
        weeks=13,
        lookback=1,
        db=db,
    )
    if finalized.get("snapshot_state") not in {"ready", "stale"}:
        raise ExportSnapshotNotReadyError("cash-flow forecast is not ready for export")
    export_id = finalized.get("export_id")
    if finalized.get("export_capture_state") != "ready" or not export_id:
        raise ExportSnapshotNotReadyError("cash-flow forecast export capture is unavailable")

    organization = organization_for_company(db, str(company_id))
    if organization is None:
        raise PermissionError("company is not mapped to an organization")
    export = load_forecast_export(
        export_id,
        organization_id=organization.id,
        expected_company_id=str(company_id),
    )
    return cashflow_report_payload_from_export(
        export,
        include_commentary=include_commentary,
    )

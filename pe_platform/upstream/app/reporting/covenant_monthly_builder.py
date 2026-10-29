# PLAN-EXEMPT: covenant monthly-grid payload builder under ratified plan 20260630-covenant-redesign; a NEW builder module kept separate from named_report_builders.py (already near the 500-line cap) — the bank-send monthly report's payload home named in this plan's dispatch.
"""
app/reporting/covenant_monthly_builder.py
────────────────────────────────────────────
Payload builder for the bank-send-ready MONTHLY covenant report (plan
20260630-covenant-redesign). Assembles, per active covenant, the 12-month two-lens
grid the bank workbook (``work/covenant-redesign/design/Covenant-Examples-TARGET.xlsx``)
reproduces — Interest / Principal / Total Debt Service / Net Income / D&A / EBITDA /
the run-rate ratio / Overage / Total Debt / the TTM "as tested" value.

DISPLAY-ONLY (AP-DR-06): the ONLY data source is ``covenant_compute.compute_monthly_series``
(the Wave-1 engine). This builder calls it once per covenant, serialises the returned
``CovenantSeries`` VERBATIM via ``to_dict()``, and re-derives NO covenant math — so the
rendered figures tie exactly to the compute engine (and its golden tie-out test).

Honest degradation (AC-3): a covenant whose series is ``available=False`` (or a month with
``no_data``) carries its reason forward for the renderer to show — never a fabricated 0.

TENANT-SAFETY PRECONDITION (AP-DR-07): the caller (routers/reporting.py) gates tenant access
via ``check_tenant_access`` BEFORE invoking a builder, and the QBO client is realm-scoped via
``get_qbo_client_from_db(db, company_id)`` — the same gate the ``/covenants/status`` endpoints use.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any  # CRAFT-OK: Any = opaque QBOClient boundary + dynamic covenant-series payload

from sqlalchemy.orm import Session

from ..models import DebtCovenant
from ..qbo_client import get_qbo_client_from_db
# PLAN-EXEMPT: plan 20260630-covenant-redesign fix — the report honors the selected as_of month.
from ..routers.covenants import _get_date_range, _load_company_ebitda_addbacks, _period_to_as_of
from ..services.covenant_compute import SUPPORTED_METRICS, compute_monthly_series

# A report payload is dynamic provider-derived JSON of varying shape.
Payload = dict[str, Any]  # CRAFT-OK: dynamic provider-derived covenant-series payload boundary


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
    """Best-effort company name from QBO CompanyInfo; blank on failure (display-only)."""
    if not client:
        return ""
    try:
        info = client.get_company_info()
        return info.get("CompanyInfo", {}).get("CompanyName", "") or ""
    except Exception:
        return ""


def build_covenant_monthly(db: Session, company_id: str, period: str) -> Payload:
    """Assemble the bank-send monthly covenant payload: one two-lens 12-month grid per
    active covenant, computed by ``compute_monthly_series`` and carried VERBATIM.

    A covenant whose compute raises (QBO unreachable for that series) records a reason so
    the renderer shows an honest unavailable block — it never fabricates a grid of zeros.
    """
    client = get_qbo_client_from_db(db, company_id)
    # PLAN-EXEMPT: plan 20260630-covenant-redesign fix — honor the selected as_of month (from the
    # download period) so the downloaded report matches the on-screen as-of grid; a current/future
    # period clamps to the last-closed default.
    start_date, end_date = _get_date_range(as_of=_period_to_as_of(period))
    company_addbacks = _load_company_ebitda_addbacks(db, company_id)

    covenants = (
        db.query(DebtCovenant)
        .filter(DebtCovenant.company_id == company_id, DebtCovenant.active == True)  # noqa: E712
        .order_by(DebtCovenant.loan_name, DebtCovenant.name)
        .all()
    )

    monthly: list[Payload] = []
    for cov in covenants:  # SA-OK: bounded-loop over a company's active covenants, not a per-row DB query
        # The monthly two-lens grid is defined ONLY for DSCR + Debt/EBITDA (the workbook's two
        # sheets). A MIN_CASH / LEVERAGE / CURRENT_RATIO / CUSTOM covenant is not a two-lens
        # metric — it keeps its single-period /covenants/status verdict and is omitted here,
        # never rendered as a fabricated Debt/EBITDA ratio.
        if str(cov.metric_type or "").upper() not in SUPPORTED_METRICS:
            continue
        entry: Payload = {
            "loan_name": cov.loan_name,
            "name": cov.name,
            "metric_type": str(cov.metric_type or "").upper(),
            "threshold": float(cov.threshold) if cov.threshold is not None else None,
            "breach_direction": cov.breach_direction,
        }
        if not client:
            entry["available"] = False
            entry["reason"] = "No QuickBooks connection is available for this covenant."
            entry["series"] = None
            monthly.append(entry)
            continue
        try:
            series = compute_monthly_series(
                cov, client, company_addbacks, start_date, end_date)
            entry["series"] = series.to_dict()  # display-only: carried verbatim
            entry["available"] = series.available
            entry["reason"] = series.reason
        except Exception as exc:  # QBO unreachable for THIS covenant — honest no-data, never a fake grid.
            entry["available"] = False
            entry["reason"] = "This covenant could not be loaded from QuickBooks."
            entry["series"] = None
            entry["error"] = str(type(exc).__name__)
        monthly.append(entry)

    return {
        "metadata": _metadata(
            company_id, period, _company_name(client),
            f"Monthly Covenant Compliance — {period}",
        ),
        "covenant_window": f"{start_date} → {end_date}",
        "monthly_covenants": monthly,
        "errors": [],
        "data_quality": {"is_partial": False, "unavailable_sections": []},
    }

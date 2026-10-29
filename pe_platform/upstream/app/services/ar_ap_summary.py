# PLAN-EXEMPT: market-ready ar-ap latency fix (Owen-ratified 2026-07-06)
"""Snapshot-first AR/AP summary engine.

GET /ar-ap/summary used to run THREE uncached QBO reports in series on the
request thread (AR aging + AP aging + the trailing-12-month P&L for DSO/DPO)
— the same live-QBO stall class the covenants W-2 F-2 and cash-flow F-3 fixes
removed. This service moves the compute off the read path:

  * ``serve_summary`` — the shared body behind GET /ar-ap/summary and
    POST /ar-ap/refresh. The GET path serves a fresh stored
    ``ARAPSummarySnapshot`` with ZERO QBO calls; on a miss/stale snapshot it
    returns the full payload SHAPE with null values and
    ``data_quality: "computing"`` (the covenants data_quality string family)
    and enqueues ONE background warm. Only the explicit refresh
    (``force_live=True``) computes on the request thread.
  * ``compute_summary_payload`` — the ONE live engine (the exact math the old
    handler ran inline); the warm task and the refresh path both call it, so
    background and foreground results cannot drift.
  * ``upsert_snapshot`` — race-safe (SAVEPOINT on the unique-conflict, the
    covenant ``_upsert_record`` pattern) idempotent write of the one
    (company, period) row.

Freshness mirrors the covenants rule (SNAPSHOT_FRESH_SECONDS = 24h): a settled
historical period is fresh at any age; the current period is fresh while its
row was updated within the TTL. The read path only ever requests the current
period, so a month rollover is simply a snapshot miss.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

from sqlalchemy.orm import Session

from ..analysis.ap_aging import ap_aging
from ..analysis.ar_aging import ar_aging
from ..models import ARAPSummarySnapshot
from ..qbo_client import get_qbo_client_from_db
from .errors import safe_error_detail
from .redaction import redact_to_string

logger = logging.getLogger(__name__)

# Mirrors covenants SNAPSHOT_FRESH_SECONDS: a current-period snapshot older
# than this is stale — the GET returns "computing" and re-warms.
SNAPSHOT_FRESH_SECONDS = 24 * 3600

_EMPTY_BUCKETS = {"current": None, "1-30": None, "31-60": None, "61-90": None, "90+": None}


def current_period() -> str:
    """The snapshot period key for an as-of-today summary read ('YYYY-MM')."""
    return date.today().strftime("%Y-%m")


# ─────────────────────────────────────────────────────────────────────────────
# Live compute (the one engine — moved verbatim from the old inline handler)
# ─────────────────────────────────────────────────────────────────────────────

def _safe_float(val, default=0.0) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _calculate_dso(total_ar: float, annual_revenue: float) -> float | None:
    """Days Sales Outstanding = (AR / Annual Revenue) × 365"""
    if annual_revenue > 0:
        return round((total_ar / annual_revenue) * 365, 1)
    return None


def _calculate_dpo(total_ap: float, annual_cogs: float) -> float | None:
    """Days Payable Outstanding = (AP / Annual COGS) × 365"""
    if annual_cogs > 0:
        return round((total_ap / annual_cogs) * 365, 1)
    return None


def _get_annual_revenue_cogs(qbo_client) -> tuple[float | None, float | None, dict | None]:
    """Pull trailing 12 month revenue and COGS from QBO P&L."""
    try:
        today = date.today()
        start = today - timedelta(days=365)
        report = qbo_client.get_report(
            "ProfitAndLoss",
            {
                "start_date": start.isoformat(),
                "end_date": today.isoformat(),
                "accounting_method": "Accrual",
            },
        )
        # Walk the report to find Total Income and Total COGS
        revenue = 0.0
        cogs = 0.0

        # QBO-OK: canonical-parser exception — this walker is LEGACY-IN-HEAD, relocated
        # VERBATIM from app/routers/receivables_payables.py (whose copy is deleted in this
        # same change; net-zero walker count). Consolidating it into app/budget/qbo_reports.py
        # is the AP-QBO-03 consolidation wave, not this latency fix.
        def _walk(rows):
            nonlocal revenue, cogs
            for row in rows:
                rtype = row.get("type", "")  # SA-OK: bounded-loop dict.get over parsed QBO report JSON rows — no DB query in this walk
                if rtype == "Section":
                    section_rows = row.get("Rows", {}).get("Row", [])
                    summary = row.get("Summary", {})

                    # Check summary for totals
                    if summary:
                        cells = summary.get("ColData", [])
                        if len(cells) >= 2:
                            label = (cells[0].get("value", "") or "").lower()
                            val = _safe_float(cells[-1].get("value", 0))
                            if "total income" in label or "total revenue" in label:
                                revenue = val
                            elif "total cost of goods" in label or "total cost of sales" in label:
                                cogs = abs(val)

                    _walk(section_rows)

        _walk(report.get("Rows", {}).get("Row", []))
        return revenue, cogs, None
    except Exception as e:
        return (
            None,
            None,
            safe_error_detail(
                error_code="ar_ap_revenue_cogs_unavailable",
                user_message="Revenue and COGS data for DSO/DPO could not be loaded.",
                exc=e,
                retryable=True,
                log_context="ar_ap_revenue_cogs_unavailable",
            ),
        )


def compute_summary_payload(db: Session, company_id: str) -> dict:
    """The live three-report compute + assembly — byte-identical to the payload
    the old inline GET handler built. Raises whatever the aging fetch raises
    (the caller owns the envelope / retry policy)."""
    qbo = get_qbo_client_from_db(db, company_id)
    ar_data = ar_aging(qbo)
    ap_data = ap_aging(qbo)

    total_ar = ar_data.get("total_ar", 0)
    total_ap = ap_data.get("total_ap", 0)
    net_position = total_ar - total_ap

    # Get revenue/COGS for DSO/DPO calculation
    annual_revenue, annual_cogs, revenue_cogs_error = _get_annual_revenue_cogs(qbo)
    dso = _calculate_dso(total_ar, annual_revenue) if annual_revenue is not None else None
    dpo = _calculate_dpo(total_ap, annual_cogs) if annual_cogs is not None else None
    errors = [revenue_cogs_error] if revenue_cogs_error else []

    # Concentration risk — largest customer/vendor as % of total
    ar_customers = ar_data.get("invoices", [])
    ap_vendors = ap_data.get("bills", [])

    top_ar_customer = max(ar_customers, key=lambda x: abs(x.get("balance", 0))) if ar_customers else None
    top_ap_vendor = max(ap_vendors, key=lambda x: abs(x.get("balance", 0))) if ap_vendors else None

    ar_concentration = None
    if top_ar_customer and total_ar > 0:
        ar_concentration = {
            "customer": top_ar_customer.get("customer", "Unknown"),
            "balance": top_ar_customer.get("balance", 0),
            "pct_of_total": round(abs(top_ar_customer.get("balance", 0)) / abs(total_ar) * 100, 1) if total_ar else 0,
        }

    ap_concentration = None
    if top_ap_vendor and total_ap > 0:
        ap_concentration = {
            "vendor": top_ap_vendor.get("vendor", "Unknown"),
            "balance": top_ap_vendor.get("balance", 0),
            "pct_of_total": round(abs(top_ap_vendor.get("balance", 0)) / abs(total_ap) * 100, 1) if total_ap else 0,
        }

    return {
        "as_of": ar_data.get("as_of"),
        "receivables": {
            "total": total_ar,
            "buckets": ar_data.get("buckets", {}),
            "customer_count": len(ar_customers),
            "dso": dso,
            "concentration": ar_concentration,
        },
        "payables": {
            "total": total_ap,
            "buckets": ap_data.get("buckets", {}),
            "vendor_count": len(ap_vendors),
            "dpo": dpo,
            "concentration": ap_concentration,
        },
        "net_position": net_position,
        "net_position_label": "net receivable" if net_position >= 0 else "net payable",
        "annual_revenue": annual_revenue,
        "annual_cogs": annual_cogs,
        "errors": errors,
        "data_quality": {
            "is_partial": bool(errors),
            "unavailable_sections": [err["error_code"] for err in errors],
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# Snapshot read / write
# ─────────────────────────────────────────────────────────────────────────────

def _snapshot_is_fresh(snapshot: ARAPSummarySnapshot, period: str) -> bool:
    """Covenants freshness rule: a settled historical period is fresh at any
    age; the current period is fresh while updated within the TTL."""
    if period < current_period():
        return True
    stamped = snapshot.updated_at or snapshot.created_at
    if stamped is None:
        return False
    if stamped.tzinfo is None:
        stamped = stamped.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - stamped).total_seconds()
    return age < SNAPSHOT_FRESH_SECONDS


def get_fresh_snapshot(db: Session, company_id: str, period: str) -> ARAPSummarySnapshot | None:
    """The (company, period) snapshot row IF it can serve the read path."""
    snapshot = (
        db.query(ARAPSummarySnapshot)
        .filter(
            ARAPSummarySnapshot.company_id == company_id,
            ARAPSummarySnapshot.period == period,
        )
        .first()
    )
    if snapshot is None or not isinstance(snapshot.payload, dict):
        return None
    return snapshot if _snapshot_is_fresh(snapshot, period) else None


def upsert_snapshot(db: Session, company_id: str, period: str, payload: dict) -> None:
    """Idempotent, race-safe write of the one (company, period) snapshot row.

    UNIQUE(company_id, period) means two concurrent warms/refreshes race on the
    INSERT; the SAVEPOINT (``begin_nested``) confines the conflict, and the
    loser re-reads the winner's row and UPDATEs it (the covenant
    ``_upsert_record`` pattern — dialect-portable across PG + sqlite)."""
    from sqlalchemy.exc import IntegrityError

    from .tenant_companies import organization_for_company

    def _existing() -> ARAPSummarySnapshot | None:
        return (
            db.query(ARAPSummarySnapshot)
            .filter(
                ARAPSummarySnapshot.company_id == company_id,
                ARAPSummarySnapshot.period == period,
            )
            .first()
        )

    existing = _existing()
    if existing is not None:
        existing.payload = payload
        existing.updated_at = datetime.now(timezone.utc)
        return

    org = organization_for_company(db, company_id)
    row = ARAPSummarySnapshot(
        organization_id=org.id if org is not None else None,
        company_id=company_id,
        period=period,
        payload=payload,
    )
    db.add(row)
    try:
        with db.begin_nested():  # SAVEPOINT — only the INSERT rolls back on a unique-conflict
            db.flush()
    except IntegrityError:
        db.expunge(row)
        winner = _existing()
        if winner is not None:
            winner.payload = payload
            winner.updated_at = datetime.now(timezone.utc)
        else:
            # Row exists at the DB level but is invisible under the current
            # scope — skip rather than crash; a correctly-scoped write heals it.
            logger.warning(
                "[ar_ap_summary] (company=%s, period=%s) conflict unresolved under current "
                "scope; snapshot not written", company_id, period,
            )


# ─────────────────────────────────────────────────────────────────────────────
# Read-path orchestration
# ─────────────────────────────────────────────────────────────────────────────

def _enqueue_warm(company_id: str, period: str) -> dict | None:
    """Enqueue the background warm. Returns a data-quality error dict when the
    broker is unreachable — a warm-enqueue failure must never 500 the read
    (mirrors cashflow ``_enqueue_snapshot_refresh``)."""
    try:
        from ..tasks.ar_ap_summary_refresh import warm_ar_ap_summary

        warm_ar_ap_summary.delay(company_id=company_id, period=period)
        return None
    except Exception as e:
        logger.warning(
            "[ar_ap_summary] warm enqueue failed for %s: %s",
            company_id, redact_to_string(str(e), max_length=300),
        )
        return safe_error_detail(
            error_code="ar_ap_summary_warm_enqueue_failed",
            user_message="The AR/AP summary refresh could not be scheduled; it will retry on the next view.",
            exc=e,
            retryable=True,
            log_context="ar_ap_summary_warm_enqueue_failed",
        )


def computing_payload(enqueue_error: dict | None = None) -> dict:
    """The full /ar-ap/summary payload SHAPE with null values and
    ``data_quality: "computing"`` — served on a snapshot miss while the
    background warm runs. Every key the snapshot-served payload carries is
    present, so the page renders without fabricated numbers."""
    errors = [enqueue_error] if enqueue_error else []
    return {
        "as_of": None,
        "receivables": {
            "total": None,
            "buckets": dict(_EMPTY_BUCKETS),
            "customer_count": 0,
            "dso": None,
            "concentration": None,
        },
        "payables": {
            "total": None,
            "buckets": dict(_EMPTY_BUCKETS),
            "vendor_count": 0,
            "dpo": None,
            "concentration": None,
        },
        "net_position": None,
        "net_position_label": None,
        "annual_revenue": None,
        "annual_cogs": None,
        "errors": errors,
        "data_quality": "computing",
    }


def serve_summary(db: Session, company_id: str, force_live: bool = False) -> dict:
    """The shared GET /ar-ap/summary body — snapshot-first, never a live QBO
    compute on the request thread except the explicit-refresh path.

    * fresh snapshot → its stored payload, zero QBO calls, no warm;
    * miss/stale → ``computing_payload`` + ONE warm enqueue;
    * ``force_live=True`` (POST /ar-ap/refresh) → live compute + snapshot rewrite.
    """
    period = current_period()

    if not force_live:
        snapshot = get_fresh_snapshot(db, company_id, period)
        if snapshot is not None:
            return snapshot.payload
        enqueue_error = _enqueue_warm(company_id, period)
        return computing_payload(enqueue_error)

    payload = compute_summary_payload(db, company_id)
    upsert_snapshot(db, company_id, period, payload)
    db.commit()
    return payload

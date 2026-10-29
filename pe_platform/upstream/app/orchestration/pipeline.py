"""
AI Orchestration Pipeline
=========================
Subscribes to DATA_SYNCED events and runs the full financial analysis pipeline.
Results are persisted to insights_* tables so the frontend never calls the AI directly.
"""
import logging
from datetime import datetime
from typing import Dict, Any

from sqlalchemy.orm import Session

from ..models import (
    SyncEvent,
    InsightKPI,
    InsightTrend,
    InsightAnomaly,
    InsightCashflow,
    ReportSection,
)
from ..qbo_client import get_qbo_client_from_db
from ..analysis.ratios import (
    resolve_period,
    compute_period_pl_kpis,
    compute_ttm_pl_kpis,
    compute_ytd_pl_kpis,
    compute_balance_sheet_kpis,
    compute_cashflow_kpis,
    compute_ratios,
    compute_trends,
)
from ..analysis.anomalies import transaction_anomalies
from ..analysis.customer_revenue import customer_revenue_summary
from ..analysis.vendor_spend import vendor_spend_summary  # noqa: F401
from ..services.errors import safe_error_detail
from ..services.redaction import redact_to_string

logger = logging.getLogger(__name__)


class _PipelineDependency(RuntimeError):
    pass


def _stage(results, name, status="succeeded", reason=None):
    results.setdefault("stages", {})[name] = {"status": status, "reason": reason}


def _record_pipeline_error(results: Dict[str, Any], key: str, user_message: str, exc: Exception) -> None:
    _stage(results, key.removesuffix("_error"), "blocked_dependency" if isinstance(exc, _PipelineDependency) else "failed", "upstream_unavailable" if isinstance(exc, _PipelineDependency) else key)
    results[key] = safe_error_detail(
        error_code=key,
        user_message=user_message,
        exc=exc,
        retryable=True,
        log_context=f"analysis_pipeline_{key}",
    )


# ---------------------------------------------------------
# Soft-close period floor (plan 20260716-five-domain-remediation, WS-A)
# ---------------------------------------------------------
def anomaly_period_floor(db: Session, realm_id: str) -> str | None:
    """The realm's SOFT-close floor (``YYYY-MM``): the latest CLOSED/LOCKED period.

    Every InsightAnomaly edge shares this one floor — detection (AC-A2) writes no
    row for a period ``<= floor``, and the read edges (Alerts Center AC-A1,
    briefing/knowledge AC-A3) FILTER rows at/below it, never delete or dismiss
    them, so a break-glass re-open resurfaces them. Delegates to the RLS-pinned
    #15-C1 lock-floor helper (lock-as-of cascade, sparse-calendar-safe; SOFT =
    ``status='CLOSED'`` counts — the auto-close cohort). Raises on a calendar
    read error: callers choose their fail-closed shape and never treat an error
    as "no floor".
    """
    from ..services.task_autonomy import latest_locked_period

    return latest_locked_period(db, realm_id=realm_id, hard_only=False)


def next_period(period: str) -> str:
    """``YYYY-MM`` plus one month (``2026-12`` → ``2027-01``)."""
    year, month = int(period[:4]), int(period[5:7])
    if month == 12:
        return f"{year + 1}-01"
    return f"{year}-{month + 1:02d}"


def resolve_open_period(db: Session, realm_id: str, candidate: str) -> str:
    """The realm's first OPEN period at or after ``candidate`` (AC-A2).

    The global nightly default (last calendar month) cannot know one realm's
    close state, so the effective analysis period is resolved PER-REALM here.
    Calendars are SPARSE (missing months carry no row) and the lock-as-of
    cascade makes everything ``<= floor`` closed, so the first open period is
    exactly ``floor`` plus one month. Raises on a floor read error (fail-loud:
    the nightly task retries; the endpoint surfaces a 503) — an indeterminate
    calendar must never resolve to a possibly-closed month.
    """
    floor = anomaly_period_floor(db, realm_id)
    if floor is None or candidate > floor:
        return candidate
    return next_period(floor)


# ---------------------------------------------------------
# Helper: clear old insights for this company + period
# ---------------------------------------------------------
def _clear_insights(db: Session, company_id: str, period: str, clear_anomalies: bool = True):
    """Remove stale insights for the given company + period before re-populating.

    Dismissed anomalies are intentionally KEPT: they are the record that the
    user dismissed a condition, and _add_anomaly_if_not_dismissed checks them
    (by fingerprint) so re-running the pipeline does not resurrect the alert.

    ``clear_anomalies=False`` (the AC-A2 closed-period gate): when the anomaly
    stage is skipped for a closed period, existing rows are left UNTOUCHED —
    deleting them would break the break-glass-reopen resurface invariant
    (AC-A1: filter, never delete), and nothing would be re-written for them.
    """
    db.query(InsightKPI).filter(
        InsightKPI.company_id == company_id,
        InsightKPI.period == period,
    ).delete()
    db.query(InsightTrend).filter(
        InsightTrend.company_id == company_id,
    ).delete()
    if clear_anomalies:
        db.query(InsightAnomaly).filter(
            InsightAnomaly.company_id == company_id,
            InsightAnomaly.period == period,
            InsightAnomaly.dismissed == False,  # noqa: E712 — keep dismissed rows
        ).delete()
    db.query(InsightCashflow).filter(
        InsightCashflow.company_id == company_id,
        InsightCashflow.period == period,
    ).delete()
    db.query(ReportSection).filter(
        ReportSection.company_id == company_id,
        ReportSection.period == period,
    ).delete()
    db.flush()


# ---------------------------------------------------------
# Helper: dismissal-aware anomaly writer
# ---------------------------------------------------------
def _add_anomaly_if_not_dismissed(
    db: Session,
    *,
    company_id: str,
    period: str,
    anomaly_type: str,
    description: str,
    severity: str,
    data_ref: dict | None,
) -> bool:
    """Add an InsightAnomaly unless the user recently dismissed the same
    underlying condition (matching fingerprint) and it has not materially
    worsened. Returns True if a row was added."""
    from ..services.alert_fingerprints import (
        anomaly_fingerprint,
        anomaly_magnitude,
        dismissed_anomaly_blocks,
    )

    fp = anomaly_fingerprint(company_id, anomaly_type, period, data_ref)
    if dismissed_anomaly_blocks(
        db,
        company_id=company_id,
        fingerprint=fp,
        new_severity=severity,
        new_magnitude=anomaly_magnitude(anomaly_type, data_ref),
    ):
        return False
    db.add(InsightAnomaly(
        company_id=company_id,
        period=period,
        anomaly_type=anomaly_type,
        description=description,
        severity=severity,
        data_ref=data_ref,
        fingerprint=fp,
    ))
    return True


# ---------------------------------------------------------
# Core Pipeline
# ---------------------------------------------------------
def run_analysis_pipeline(db: Session, company_id: str, period: str) -> Dict[str, Any]:
    """
    Master analysis pipeline. Called after a DATA_SYNCED event.

    Steps:
      1. Resolve the period into date windows.
      2. Compute KPIs (period, TTM, YTD, BS, CF, ratios).
      3. Compute trends (12-month revenue + EBITDA series).
      4. Run anomaly detection.
      5. Compute customer/vendor concentrations.
      6. Persist everything into insights_* and report_sections tables.

    Returns a summary dict for logging / API response.
    """
    logger.info(f"=== Pipeline starting for {company_id} period={period} ===")

    results: Dict[str, Any] = {"company_id": company_id, "period": period, "stages": {}}
    # An explicit malformed period must not silently resolve to today's month.
    try:
        parts = period.split("-")
        if len(period) != 7 or len(parts) != 2:
            raise ValueError("Expected YYYY-MM")
        year, month = int(parts[0]), int(parts[1])
        if not 1 <= month <= 12 or not 1 <= year <= 9999:
            raise ValueError("Period out of range")
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("analysis_period_invalid") from exc
    client = get_qbo_client_from_db(db, company_id)
    resolved = resolve_period(year, month)

    # Soft-close gate (AC-A2): resolve the floor ONCE. The anomaly stage below
    # writes NOTHING for a period at/below the floor. Fail-CLOSED (mirrors
    # period_is_closed): an indeterminate calendar read gates the stage —
    # never write into a possibly-closed period.
    try:
        period_floor = anomaly_period_floor(db, company_id)
        anomaly_stage_open = period_floor is None or period > period_floor
        _stage(results, "period_lock")
    except Exception as e:
        logger.warning(
            "Anomaly period-floor read failed for %s; failing CLOSED (anomaly stage skipped): %s",
            company_id,
            redact_to_string(str(e), max_length=500),
        )
        period_floor = None
        anomaly_stage_open = False
        _stage(results, "period_lock", "unavailable", "period_lock_unavailable")

    # Clear stale insights (anomaly rows are left untouched when the stage is
    # gated — see _clear_insights docstring).
    _clear_insights(db, company_id, period, clear_anomalies=anomaly_stage_open)


    # --------------------------------------------------
    # 1. P&L KPIs (current period + TTM + YTD)
    # --------------------------------------------------
    try:
        period_pl = compute_period_pl_kpis(client, resolved)
        ttm_pl = compute_ttm_pl_kpis(client, resolved)
        ytd_pl = compute_ytd_pl_kpis(client, resolved)

        kpi_rows = [
            ("revenue", period_pl["revenue"], "USD"),
            ("gross_profit", period_pl["gross_profit"], "USD"),
            ("ebitda", period_pl["ebitda"], "USD"),
            ("net_income", period_pl["net_income"], "USD"),
            ("ttm_revenue", ttm_pl["ttm_revenue"], "USD"),
            ("ttm_gross_profit", ttm_pl["ttm_gross_profit"], "USD"),
            ("ttm_ebitda", ttm_pl["ttm_ebitda"], "USD"),
            ("ttm_gross_margin_pct", ttm_pl["ttm_gross_margin_pct"], "%"),
            ("ttm_ebitda_ratio", ttm_pl["ttm_ebitda_ratio"], "%"),
            ("ytd_sales", ytd_pl["sales"], "USD"),
            ("ytd_ebitda", ytd_pl["ebitda"], "USD"),
        ]

        for name, value, unit in kpi_rows:
            db.add(InsightKPI(
                company_id=company_id, period=period,
                name=name, value=value, unit=unit,
            ))

        results["period_pl"] = period_pl
        results["ttm_pl"] = ttm_pl
        _stage(results, "period_pl")
    except Exception as e:
        _record_pipeline_error(results, "period_pl_error", "P&L KPI data could not be computed.", e)

    # --------------------------------------------------
    # 2. Balance Sheet KPIs
    # --------------------------------------------------
    try:
        bs = compute_balance_sheet_kpis(client, resolved["end_date"])

        bs_kpis = [
            ("cash", bs["cash"], "USD"),
            ("ar", bs["ar"], "USD"),
            ("total_assets", bs["total_assets"], "USD"),
            ("total_liabilities", bs["total_liabilities"], "USD"),
            ("total_equity", bs["total_equity"], "USD"),
            ("current_assets", bs["current_assets"], "USD"),
            ("current_liabilities", bs["current_liabilities"], "USD"),
            ("net_working_capital", bs["current_assets"] - bs["current_liabilities"], "USD"),
        ]
        for name, value, unit in bs_kpis:
            db.add(InsightKPI(
                company_id=company_id, period=period,
                name=name, value=value, unit=unit,
            ))

        results["bs"] = bs
        _stage(results, "bs")
    except Exception as e:
        _record_pipeline_error(results, "bs_error", "Balance sheet KPI data could not be computed.", e)

    # --------------------------------------------------
    # 3. Cash Flow KPIs
    # --------------------------------------------------
    try:
        cf = compute_cashflow_kpis(client, resolved)

        for cat in ["cfo", "cfi", "cff", "net_cash_flow"]:
            db.add(InsightCashflow(
                company_id=company_id, period=period,
                category=cat, value=cf.get(cat, 0),
            ))

        results["cf"] = cf
        _stage(results, "cf")
    except Exception as e:
        _record_pipeline_error(results, "cf_error", "Cash flow KPI data could not be computed.", e)

    # --------------------------------------------------
    # 4. Ratios
    # --------------------------------------------------
    try:
        if "period_pl" in results and "bs" in results:
            ratios = compute_ratios(results["period_pl"], results["bs"])
            for name, value in ratios.items():
                db.add(InsightKPI(
                    company_id=company_id, period=period,
                    name=name, value=value, unit="ratio",
                ))
            results["ratios"] = ratios
            _stage(results, "ratios")
        else:
            raise _PipelineDependency("P&L and balance sheet required")
    except Exception as e:
        _record_pipeline_error(results, "ratios_error", "Ratios could not be completed.", e)

    # --------------------------------------------------
    # 5. Trends (12-month series)
    # --------------------------------------------------
    try:
        months, rev_vals, ebitda_vals, gp_vals = compute_trends(client, resolved)

        for i, m in enumerate(months):
            if i < len(rev_vals):
                db.add(InsightTrend(
                    company_id=company_id, metric="revenue",
                    period=m, value=rev_vals[i],
                ))
            if i < len(ebitda_vals):
                db.add(InsightTrend(
                    company_id=company_id, metric="ebitda",
                    period=m, value=ebitda_vals[i],
                ))

        results["trend_months"] = len(months)
        _stage(results, "trends")
    except Exception as e:
        _record_pipeline_error(results, "trends_error", "Trend data could not be computed.", e)

    # --------------------------------------------------
    # 6. Anomaly Detection (comprehensive)
    # --------------------------------------------------

    def _severity_from_z(z: float) -> str:
        if z >= 4.0:
            return "critical"
        elif z >= 3.0:
            return "warning"
        return "info"

    if not anomaly_stage_open:
        # AC-A2: the period is at/below the SOFT-close floor (or the floor read was
        # indeterminate) — detection is skipped entirely, so no InsightAnomaly row
        # is ever written for a closed period. Existing rows were NOT cleared above:
        # the read edges hide them via the same floor and a break-glass re-open
        # resurfaces them (filter-never-delete, AC-A1).
        unknown_lock = results["stages"]["period_lock"]["status"] == "unavailable"
        for name in ("anomalies_txn", "anomalies_cogs", "negative_balance", "revenue_concentration", "ar_aging"):
            _stage(results, name, "unavailable" if unknown_lock else "skipped_policy", "period_lock_unavailable" if unknown_lock else "closed_period")
        if not unknown_lock:
            results["anomaly_stage_skipped_closed_period"] = period
        results["total_anomalies_written"] = 0
        logger.info(
            "Anomaly stage skipped for %s: period %s is at/below the soft-close floor (%s).",
            company_id, period, period_floor,
        )
    else:
        # 6a. Transaction anomalies (two-sided modified z-score on Invoice/Purchase amounts)
        try:
            # Threshold re-based (plan 20260711-peregrine-finance-os FOS-1 AC-1.5): the old
            # z_threshold=2.5 was tuned to the removed one-sided mean+pstdev z-scale; the
            # detector is now two-sided median+MAD modified-z (Iglewicz-Hoaglin standard 3.5,
            # the detector default). Carrying 2.5 onto the new scale would be substantially
            # NOISIER, not equivalent, so we take the default.
            # AC-A2: bound the evidence window to open periods — rows dated at/below
            # the floor never enter the population.
            txn_date_from = f"{next_period(period_floor)}-01" if period_floor else None
            txn_result = transaction_anomalies(client, limit=500, txn_date_from=txn_date_from)
            # transaction_anomalies returns a DICT with key "anomalies"
            txn_anomalies_list = txn_result.get("anomalies", []) if isinstance(txn_result, dict) else []
            for item in txn_anomalies_list:
                # z_score is now SIGNED (negative = low-side outlier); severity is a
                # MAGNITUDE judgment, so rank on |z| — a z of -34 is critical, not info.
                z = item.get("z_score", 0)  # SA-OK: bounded-loop dict.get on an in-memory anomaly item, not a DB query
                _add_anomaly_if_not_dismissed(
                    db, company_id=company_id, period=period,
                    anomaly_type="unusual_transaction",
                    description=f"{item.get('type', 'Transaction')} #{item.get('id', '?')} with {item.get('name', 'unknown')} "
                                f"for ${item.get('amount', 0):,.2f} (modified z-score: {z:.1f})",  # SA-OK: bounded-loop dict.get on an in-memory anomaly item, not a DB query
                    severity=_severity_from_z(abs(z)),
                    data_ref=item,
                )
            results["anomalies_txn"] = len(txn_anomalies_list)
            _stage(results, "anomalies_txn")
        except Exception as e:
            _record_pipeline_error(
                results,
                "anomalies_txn_error",
                "Transaction anomaly detection could not be completed.",
                e,
            )

        # 6b. COGS anomalies (z-score on monthly COGS; AC-A2: months at/below the
        # floor are never flagged)
        try:
            from ..analysis.cogs_anomaly import cogs_anomalies
            cogs_result = cogs_anomalies(client, z_threshold=2.0, period_floor=period_floor)
            cogs_anomalies_list = cogs_result.get("anomalies", []) if isinstance(cogs_result, dict) else []
            for item in cogs_anomalies_list:
                z = item.get("z_score", 0)  # SA-OK: bounded-loop dict.get on an in-memory anomaly item, not a DB query
                _add_anomaly_if_not_dismissed(
                    db, company_id=company_id, period=period,
                    anomaly_type="cogs_spike",
                    description=f"COGS spike in {item.get('month', '?')}: ${item.get('cogs', 0):,.2f} (z-score: {z:.1f})",  # SA-OK: bounded-loop dict.get on an in-memory anomaly item, not a DB query
                    severity=_severity_from_z(z),
                    data_ref=item,
                )
            results["anomalies_cogs"] = len(cogs_anomalies_list)
            _stage(results, "anomalies_cogs")
        except Exception as e:
            _record_pipeline_error(
                results,
                "anomalies_cogs_error",
                "COGS anomaly detection could not be completed.",
                e,
            )

        # 6c. Negative bank balance
        try:
            if "bs" not in results or "cash" not in results["bs"]:
                raise _PipelineDependency("Balance sheet cash required")
            cash = results["bs"]["cash"]
            if cash < 0:
                _add_anomaly_if_not_dismissed(
                    db, company_id=company_id, period=period,
                    anomaly_type="negative_balance",
                    description=f"Negative bank balance: ${cash:,.2f} as of {resolved['end_date']}",
                    severity="critical",
                    data_ref={"cash": cash, "as_of": resolved["end_date"]},
                )
            _stage(results, "negative_balance")
        except Exception as e:
            _record_pipeline_error(results, "negative_balance_error", "Negative balance check could not be completed.", e)

        # 6d. Revenue concentration risk
        try:
            if "ttm_pl" not in results:
                raise _PipelineDependency("TTM revenue required")
            cr = customer_revenue_summary(client, limit=1000)
            ttm_rev = results.get("ttm_pl", {}).get("ttm_revenue", 0)
            if ttm_rev and cr.get("top_5_customers"):
                top5_total = sum(v for _, v in cr["top_5_customers"])
                concentration = top5_total / ttm_rev * 100
                if concentration > 80:
                    _add_anomaly_if_not_dismissed(
                        db, company_id=company_id, period=period,
                        anomaly_type="revenue_concentration",
                        description=f"Top 5 customers = {concentration:.0f}% of revenue — high concentration risk",
                        severity="critical" if concentration > 90 else "warning",
                        data_ref={"concentration_pct": concentration, "top5_revenue": top5_total, "ttm_revenue": ttm_rev},
                    )
                elif concentration > 50:
                    _add_anomaly_if_not_dismissed(
                        db, company_id=company_id, period=period,
                        anomaly_type="revenue_concentration",
                        description=f"Top 5 customers = {concentration:.0f}% of revenue — moderate concentration risk",
                        severity="info",
                        data_ref={"concentration_pct": concentration, "top5_revenue": top5_total, "ttm_revenue": ttm_rev},
                    )
            _stage(results, "revenue_concentration")
        except Exception as e:
            _record_pipeline_error(results, "revenue_concentration_error", "Revenue concentration check could not be completed.", e)

        # 6e. AR aging past 90 days
        try:
            from ..analysis.ar_aging import ar_aging
            ar_data = ar_aging(client, limit=2000)
            ar_90_plus = ar_data.get("buckets", {}).get("90+", 0)
            ar_total = ar_data.get("total_ar", 0)
            if ar_90_plus != 0 and abs(ar_90_plus) > 1000:
                pct = (ar_90_plus / ar_total * 100) if ar_total else 0
                _add_anomaly_if_not_dismissed(
                    db, company_id=company_id, period=period,
                    anomaly_type="ar_aging_90plus",
                    description=f"${ar_90_plus:,.2f} in AR past 90 days ({pct:.0f}% of total AR)",
                    severity="critical" if pct > 30 else "warning",
                    data_ref={"ar_90_plus": ar_90_plus, "ar_total": ar_total, "pct": pct},
                )
            _stage(results, "ar_aging")
        except Exception as e:
            _record_pipeline_error(results, "ar_aging_error", "AR aging check could not be completed.", e)

        # The session is autoflush=False (db.py:65): flush so the count below sees
        # the rows just added by the detectors (they were invisible pre-flush).
        db.flush()
        results["total_anomalies_written"] = db.query(InsightAnomaly).filter(
            InsightAnomaly.company_id == company_id,
            InsightAnomaly.period == period,
            InsightAnomaly.dismissed == False,  # noqa: E712 — retained dismissed rows aren't "written"
        ).count()
        logger.info(f"Anomalies written: {results['total_anomalies_written']}")

    # --------------------------------------------------
    # 7. Report Sections (pre-bake for PDF generation)
    # --------------------------------------------------
    try:
        if any(name not in results for name in ("period_pl", "bs", "ttm_pl", "cf", "ratios")):
            raise _PipelineDependency("Report section sources incomplete")
        sections = [
            ("FRONT_PAGE", {"kpis": results.get("period_pl", {}), "bs": results.get("bs", {})}, 1),
            ("CURRENT_PL", results.get("period_pl", {}), 2),
            ("CURRENT_BS", results.get("bs", {}), 3),
            ("TTM_PL", results.get("ttm_pl", {}), 4),
            ("TTM_CF", results.get("cf", {}), 5),
            ("RATIOS", results.get("ratios", {}), 6),
        ]
        for section_type, content, order in sections:
            db.add(ReportSection(
                company_id=company_id, period=period,
                section_type=section_type, content=content,
                order_index=order,
            ))
        _stage(results, "report_sections")
    except Exception as e:
        _record_pipeline_error(results, "report_sections_error", "Report sections could not be completed.", e)

    # --------------------------------------------------
    # 8. Passive Knowledge Extraction
    # --------------------------------------------------
    try:
        # Flush the session so that the newly added KPIs and Anomalies are 
        # readable by the extraction engine's query
        db.flush()
        
        if any(name not in results for name in ("period_pl", "bs")):
            raise _PipelineDependency("Knowledge source coverage incomplete")
        from .knowledge_extraction import extract_passive_knowledge
        new_facts_count = extract_passive_knowledge(db, company_id, period)
        results["new_knowledge_inferred"] = new_facts_count
        _stage(results, "knowledge")
    except Exception as e:
        _record_pipeline_error(results, "knowledge_error", "Passive knowledge extraction could not be completed.", e)

    # --------------------------------------------------
    # Commit all insights
    # --------------------------------------------------
    db.commit()

    states = [stage["status"] for stage in results["stages"].values()]
    incomplete = any(state in ("failed", "blocked_dependency", "unavailable") for state in states)
    useful = any(results["stages"].get(name, {}).get("status") == "succeeded" for name in ("period_pl", "bs", "cf", "trends", "anomalies_txn", "anomalies_cogs", "ar_aging"))
    results["status"] = "partial" if incomplete and useful else "failed" if incomplete else "succeeded"
    results["failed_stages"] = [name for name, stage in results["stages"].items() if stage["status"] in ("failed", "blocked_dependency", "unavailable")]
    logger.info(f"=== Pipeline complete for {company_id}. ===")
    return results


# ---------------------------------------------------------
# Event-driven entrypoint (picks up PENDING SyncEvents)
# ---------------------------------------------------------
def process_pending_events(db: Session, max_runs: int = 10):
    """
    Scans for PENDING SyncEvents and runs the pipeline for each.
    Can be called from a Celery beat task or a cron job.

    First-run safety bounds (the beat task that calls this never ran in prod,
    so months of PENDING events may have accumulated):
      * Duplicate events for the same (company_id, period) are collapsed to
        the newest one — the pipeline recomputes the whole period from
        scratch, so older duplicates are pure rework. They are marked
        SUPERSEDED without running.
      * At most ``max_runs`` pipeline runs per invocation; the beat task
        fires every 5 minutes, so any backlog drains incrementally instead
        of in one unbounded pass.
    """
    from ..tenant_rls import clear_tenant_scope, set_tenant_scope, tenant_scope
    from ..services.tenant_companies import organization_for_company

    # Cross-tenant system scan of PENDING events across all realms (RLS bypass);
    # each per-event pipeline run below is re-pinned to that event's realm.
    # set (emit-next-begin) is CORRECT here, NOT pin (emit-now): `db` arrives fresh
    # (process_pending_sync_events opens a new SessionLocal and runs no query before
    # this call), so the FIRST query below (`pending = …`) triggers the transaction
    # begin and the begin-listener emits this bypass scope onto it. Do NOT convert
    # to pin_tenant_scope_live — the INCIDENT-2 bug is set-on-an-ALREADY-OPEN txn.
    set_tenant_scope(bypass=True)

    pending = (
        db.query(SyncEvent)
        .filter(SyncEvent.status == "PENDING")
        .order_by(SyncEvent.created_at.desc(), SyncEvent.id.desc())
        .all()
    )

    latest_by_key: Dict[Any, SyncEvent] = {}
    superseded = []
    for event in pending:
        key = (event.company_id, event.period)
        if key in latest_by_key:
            superseded.append(event)
        else:
            latest_by_key[key] = event

    for event in superseded:
        event.status = "SUPERSEDED"
        event.completed_at = datetime.utcnow()
    if superseded:
        db.commit()

    events = list(latest_by_key.values())[:max_runs]
    logger.info(
        "Found %s pending sync events: running %s, superseded %s duplicate(s), %s deferred to next run.",
        len(pending),
        len(events),
        len(superseded),
        max(len(latest_by_key) - len(events), 0),
    )

    for event in events:
        # Pin RLS to this event's tenant for its pipeline run. §B: pin org+realm
        # (the pipeline touches org-keyed tables); plain ContextVar pin preserves
        # the existing commit-then-emit timing (the db.commit() below resets the
        # txn so run_analysis_pipeline emits the pinned scope cleanly).
        event_org = organization_for_company(db, event.company_id)
        with tenant_scope(org_id=event_org.id if event_org else None, realm_id=event.company_id):
            event.status = "PROCESSING"
            db.commit()

            try:
                result = run_analysis_pipeline(db, event.company_id, event.period)
                event.status = {"succeeded": "DONE", "partial": "PARTIAL", "failed": "FAILED"}.get(result.get("status"), "PARTIAL")
                event.error_message = ", ".join(result.get("failed_stages", [])) or None
                event.completed_at = datetime.utcnow()
            except Exception as e:
                logger.error(
                    "Pipeline failed for event #%s: %s",
                    event.id,
                    redact_to_string(str(e), max_length=500),
                )
                db.rollback()
                event.status = "FAILED"
                event.error_message = "analysis_pipeline_failed"
                event.completed_at = datetime.utcnow()

            db.commit()

    clear_tenant_scope()

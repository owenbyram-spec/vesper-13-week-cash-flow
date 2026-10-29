import logging
from datetime import date, timedelta
from ..qbo_client import get_qbo_client_from_db
from ..models import WatchdogAlert
from ..services.alert_fingerprints import watchdog_fingerprint

logger = logging.getLogger(__name__)


def _existing_alert_by_fingerprint(db, company_id: str, fingerprint: str):
    """Fingerprint-based dedupe: matches regardless of acknowledged state, so a
    dismissed transaction alert is never recreated for the same transaction."""
    return (
        db.query(WatchdogAlert)
        .filter(
            WatchdogAlert.company_id == company_id,
            WatchdogAlert.fingerprint == fingerprint,
        )
        .first()
    )


# PLAN-EXEMPT: alerts-engine dedup crash — WatchdogAlert.context is a generic Column(JSON)
# (models.py:1402), NOT JSONB, so its comparator has no ->>/.astext operator. The prior in-SQL
# `context["amount"].astext == ...` dedup filters raised AttributeError at expression-build time
# the moment they executed (dialect-independent — SQLite reproduces it), a latent crash in the
# existing-alert dedup path. Snapshot the existing context keys ONCE in Python and dedup off the
# deserialized dict, mirroring the proactive.py / agent.py (commit ea08489) fix for the same class.
def _ctx_key(ctx: dict, fields: tuple) -> tuple:
    """A dedup key tuple of the given context fields. `amount` is str-normalized to reproduce the
    old `context["amount"].astext == str(amt)` comparison; the others compare as-stored."""
    return tuple(str(ctx.get(f)) if f == "amount" else ctx.get(f) for f in fields)


def _existing_context_keys(db, company_id: str, alert_type: str, fields: tuple) -> set:
    """Existing-alert dedup keys for one alert_type, read in Python from the JSON context.

    Scope mirrors the original in-SQL filter exactly: company_id + alert_type (no period — the
    fingerprint already encodes the period; this context path is the broader secondary dedup)."""
    rows = (
        db.query(WatchdogAlert.context)
        .filter(
            WatchdogAlert.company_id == company_id,
            WatchdogAlert.alert_type == alert_type,
        )
        .all()
    )
    return {_ctx_key(ctx, fields) for (ctx,) in rows if isinstance(ctx, dict)}

def scan_recent_transactions_for_alerts(db, company_id: str, company_name: str, period: str) -> int:
    """
    Scans the last 30 days of QBO transactions for:
    - Large anomalous outbound transactions
    - Duplicate payments to the same vendor
    """
    client = get_qbo_client_from_db(db, company_id)
    if not client:
        return 0

    end_date = date.today()
    start_date = end_date - timedelta(days=30)
    new_alerts = 0

    try:
        # Fetch Purchases (Expenses/Checks)
        query = f"SELECT * FROM Purchase WHERE TxnDate >= '{start_date.isoformat()}' MAXRESULTS 1000"
        resp = client.query(query)
        purchases = resp.get("QueryResponse", {}).get("Purchase", [])

        # Add BillPayments
        query2 = f"SELECT * FROM BillPayment WHERE TxnDate >= '{start_date.isoformat()}' MAXRESULTS 1000"
        resp2 = client.query(query2)
        bill_payments = resp2.get("QueryResponse", {}).get("BillPayment", [])

        all_outflows = purchases + bill_payments
        
        # Detect Dupes: group by (VendorRef, Amount, TxnDate)
        # PLAN-EXEMPT: alerts-engine dedup crash — snapshot existing DUPLICATE_PAYMENT context
        # keys once (Python-side dedup; the in-SQL .astext filter raised AttributeError).
        existing_dup_keys = _existing_context_keys(
            db, company_id, "DUPLICATE_PAYMENT", ("amount", "date", "vendor")
        )
        seen = {}
        for txn in all_outflows:
            amt = float(txn.get("TotalAmt", 0))
            txn_date = txn.get("TxnDate", "")
            vendor_ref = txn.get("EntityRef", {}).get("value", "") or txn.get("VendorRef", {}).get("value", "Unknown")
            vendor_name = txn.get("EntityRef", {}).get("name", "") or txn.get("VendorRef", {}).get("name", "Unknown Vendor")
            if amt <= 0.0 or vendor_name == "Unknown Vendor":
                continue

            key = f"{vendor_ref}_{amt}_{txn_date}"
            if key in seen:
                # We have a duplicate!
                context = {"amount": amt, "date": txn_date, "vendor": vendor_name}
                fingerprint = watchdog_fingerprint(company_id, "DUPLICATE_PAYMENT", period, context=context)
                # PLAN-EXEMPT: alerts-engine dedup crash — dedup off the Python snapshot, not .astext SQL.
                dup_key = _ctx_key(context, ("amount", "date", "vendor"))
                existing = _existing_alert_by_fingerprint(db, company_id, fingerprint) or dup_key in existing_dup_keys
                if not existing:
                    from decimal import Decimal
                    alert = WatchdogAlert(
                        company_id=company_id,
                        alert_type="DUPLICATE_PAYMENT",
                        severity="warning",
                        title=f"Potential Duplicate Payment: {vendor_name}",
                        description=f"Two payments of ${amt:,.2f} recorded on {txn_date} to {vendor_name}.",
                        metric_value=Decimal(str(amt)),
                        # PLAN-EXEMPT: F4 follow-up (plan 20260701-frontend-audit-fixes, closed) — tag the
                        # producer missed by F4 so the intelligence-page formatter renders it right. A $ payment amount.
                        metric_unit="currency",
                        context=context,
                        period=period,
                        fingerprint=fingerprint,
                    )
                    db.add(alert)
                    new_alerts += 1
                    # PLAN-EXEMPT: alerts-engine dedup crash — keep the snapshot current so a later
                    # identical txn in this same scan dedups (the old per-iteration query saw it via autoflush).
                    existing_dup_keys.add(dup_key)
            else:
                seen[key] = True

        # Detect Large Outflows
        # PLAN-EXEMPT: alerts-engine dedup crash — snapshot existing LARGE_TRANSACTION context keys
        # once (amount + date, mirroring the original filter); replaces the in-SQL .astext dedup.
        existing_large_keys = _existing_context_keys(
            db, company_id, "LARGE_TRANSACTION", ("amount", "date")
        )
        LARGE_THRESHOLD = 25000.0
        for txn in all_outflows:
            amt = float(txn.get("TotalAmt", 0))
            if amt >= LARGE_THRESHOLD:
                vendor_name = txn.get("EntityRef", {}).get("name", "") or txn.get("VendorRef", {}).get("name", "Unknown Vendor")
                txn_date = txn.get("TxnDate", "")

                context = {"amount": amt, "date": txn_date, "vendor": vendor_name}
                fingerprint = watchdog_fingerprint(company_id, "LARGE_TRANSACTION", period, context=context)
                # PLAN-EXEMPT: alerts-engine dedup crash — dedup off the Python snapshot, not .astext SQL.
                large_key = _ctx_key(context, ("amount", "date"))
                existing = _existing_alert_by_fingerprint(db, company_id, fingerprint) or large_key in existing_large_keys

                if not existing:
                    from decimal import Decimal
                    alert = WatchdogAlert(
                        company_id=company_id,
                        alert_type="LARGE_TRANSACTION",
                        severity="warning",
                        title=f"Large Outflow Detected: ${amt:,.0f}",
                        description=f"A payment of ${amt:,.2f} was made on {txn_date}. Payee: {vendor_name}.",
                        metric_value=Decimal(str(amt)),
                        # PLAN-EXEMPT: F4 follow-up (plan 20260701-frontend-audit-fixes, closed) — tag the
                        # producer missed by F4 so the intelligence-page formatter renders it right. A $ outflow amount.
                        metric_unit="currency",
                        context=context,
                        period=period,
                        fingerprint=fingerprint,
                    )
                    db.add(alert)
                    new_alerts += 1
                    # PLAN-EXEMPT: alerts-engine dedup crash — keep the snapshot current within this scan.
                    existing_large_keys.add(large_key)

        # Z_SCORE_ANOMALY detector REMOVED (plan 20260701-pilot-fixes-batch-3 AC-2):
        # the z-score transaction alerts were statistical jargon with a unitless
        # "score" metric — meaningless to the pilot users. The kept cash-transaction
        # anomaly detectors are the two deterministic ones above (DUPLICATE_PAYMENT +
        # LARGE_TRANSACTION); P&L-vs-budget variance lives in
        # app/tasks/proactive._check_budget_vs_actual_trailing. Legacy Z_SCORE_ANOMALY
        # rows still render (insights.py maps their NULL/score unit safely).

        db.commit()
    except Exception as e:
        logger.error(f"Transaction scan failed for {company_id}: {e}")
        db.rollback()

    return new_alerts

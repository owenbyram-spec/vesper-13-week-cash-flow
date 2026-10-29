"""
app/routers/cashflow_forecast.py
──────────────────────────────────
Rolling 13-week cash flow forecast — invoice-driven + pattern fallback.

Primary data source: Open Invoices (AR) and open Bills (AP) from QBO,
placed into weekly columns using per-entity DSO/DPO behavioral offsets.

Fallback: For entities with no open invoices/bills (recurring payments
that don't generate invoices — e.g. payroll, rent), falls back to the
original pattern-based projection from 6 months of transaction history.

GET    /cashflow-forecast/weekly         — 13-week forecast grid
POST   /cashflow-forecast/override       — save a user cell override
DELETE /cashflow-forecast/override/{id}  — remove an override
"""
from __future__ import annotations

import calendar
import logging
from collections import defaultdict
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import (
    CashForecastActual,
    CashForecastOverride,
    CashForecastSnapshot,
    CashForecastWeekStatus,
    User,
)
from ..auth import get_current_user_from_token
from ..cashflow.projection import (
    AVG_SENTINEL,
    BALANCE_SENTINEL,
    DAY_SENTINEL,
    HIDDEN_SENTINEL,
    MONTHLY,
    ORDER_SENTINEL,
    _aggregate_entity_patterns,
    _combined_run_rate_pattern_map,
    _infer_run_rate_patterns_from_open_items,
    _median_int,  # noqa: F401
    _month_key,
    _project_into_weeks,
    _project_invoices_into_weeks,
    project_interval_amounts,
)
from ..cashflow.qbo_cash import (
    _amount_from_col_data,  # noqa: F401
    _bank_accounts_section_detail,  # noqa: F401
    _bank_accounts_section_total,  # noqa: F401
    _canonical_account_label,  # noqa: F401
    _cash_account_universe,  # noqa: F401
    _cash_account_universe_for_date,  # noqa: F401
    _fetch_active_qbo_accounts,  # noqa: F401
    _historical_cash_balance,
    _historical_cash_balance_detail,
    _is_bank_accounts_section,  # noqa: F401
    _is_clearing_account_name,  # noqa: F401
    _label_from_col_data,  # noqa: F401
    _parse_amt,  # noqa: F401
    _report_amount_column_title,  # noqa: F401
    _report_data_row_amounts,  # noqa: F401
    _report_total_amount_index,  # noqa: F401
    _sum_report_data_rows,  # noqa: F401
)
from ..cashflow.actuals_pull import (
    CASCADE_READY_WEEK_STATUSES,
    WEEK_STATUS_CLOSED,
    WEEK_STATUS_NOT_PULLED,
    WEEK_STATUS_PENDING,
    WEEK_STATUS_PULLED,
    NoCashAccountsError,
    _actual_record_payload,
    _actuals_counts_for_week,
    _sync_week_status,
    pull_week_actuals,
)
from ..cashflow.liquidity import liquidity_from_payload
from ..cashflow.qbo_paging import query_all_pages, txn_date_where
from ..cashflow.export_snapshots import (
    capture_forecast_export,
    content_from_weekly_payload,
    resolve_company_display_name,
)
from ..cashflow.schemas import FORECAST_EXPORT_SCHEMA_VERSION
from ..cashflow.scenarios import build_scenario_set
from ..cashflow.variance import build_week_variance_report, compute_forecast_accuracy
from ..cashflow.versioning import (
    actuals_by_entity_for_weeks,
    collect_scored_weeks,
    freeze_snapshot_version,
    version_for_target_week,
)
from ..dependencies import check_entity_tenant_access, check_tenant_access, global_tenant_guard
from ..qbo_client import get_qbo_client_from_db
from ..analysis.payment_behavior import (
    compute_customer_dso,
    compute_vendor_dpo,
    derive_collection_haircuts,
)
# PLAN-EXEMPT: ratified plan 20260702-qa-remediation W-5 (F-4 — CFF integrity imports).
from ..services.cff_integrity import (
    DEFAULT_SINCE_WEEKS as CFF_DEFAULT_SINCE_WEEKS,
    SKIP_ENTITY_NAMES as CFF_SKIP_ENTITY_NAMES,
    ForecastRowIndex,
    list_unmapped_categorized_entities,
    load_forecast_row_index,
)
from ..services.errors import safe_error_detail, user_safe_http_exception
from ..services.redaction import redact_to_string
from ..services.tenant_companies import organization_for_company

from datetime import datetime as _datetime, timezone

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/cashflow-forecast", tags=["Cash Flow Forecast"])

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _next_friday(d: date) -> date:
    """Return the next Friday on or after *d* (week-ending convention)."""
    days_ahead = 4 - d.weekday()  # Friday = 4
    if days_ahead < 0:
        days_ahead += 7
    return d + timedelta(days=days_ahead)


def _week_fridays(n: int = 13, lookback: int = 0) -> List[date]:
    """Return *lookback* prior Fridays + the next *n* Friday week-ending dates."""
    start = _next_friday(date.today())
    prior = [start - timedelta(weeks=i) for i in range(lookback, 0, -1)]
    future = [start + timedelta(weeks=i) for i in range(n)]
    return prior + future


def _week_index(d: date, fridays: List[date]) -> Optional[int]:
    """Return the column index (0-based) for a given date within the grid."""
    for i, fri in enumerate(fridays):
        mon = fri - timedelta(days=4)
        if mon <= d <= fri:
            return i
    return None


def _to_date(val) -> date:
    if isinstance(val, str):
        return date.fromisoformat(val[:10])
    if hasattr(val, "date"):
        return val.date()
    return val


def _safe_date(val) -> "date | None":
    # PLAN-EXEMPT: 2026-07-09 honest-failure batch (AC-C4) — narrowed from a bare
    # `except Exception`; the contract is "parse or None" for malformed stored week
    # headers only, and a narrow except keeps genuine failures loud.
    try:
        return _to_date(val)
    except (ValueError, TypeError, AttributeError):
        return None


# ─────────────────────────────────────────────────────────────────────────────
# QBO Data Fetching — Historical Analysis + Invoice/Bill Data
# ─────────────────────────────────────────────────────────────────────────────

# Collection haircuts are derived PER-TENANT from QBO Payment↔Invoice history
# (app/analysis/payment_behavior.derive_collection_haircuts) — the fixed universal
# table that lived here is retired (plan 20260706-cfo-grade-financial-workflows AC-3;
# gap G-2: no defensible published constant exists). When a bucket has insufficient
# history its rate is None and the invoice is carried at FACE VALUE with the basis
# labeled — an honest unhaircut amount, never a fabricated rate.
NO_HAIRCUT = 1.0


def _collection_haircut(days_past_due: int, haircut_buckets: dict[int, float | None] | None) -> float:
    """Per-tenant collection probability for an invoice's aging bucket.

    ``haircut_buckets`` maps bucket floors (0/31/61/91 days past due) to empirical
    collection rates or None (thin history). Missing curve or bucket ⇒ face value.
    """
    if not haircut_buckets:
        return NO_HAIRCUT
    if days_past_due <= 30:
        floor = 0
    elif days_past_due <= 60:
        floor = 31
    elif days_past_due <= 90:
        floor = 61
    else:
        floor = 91
    rate = haircut_buckets.get(floor)
    return float(rate) if rate is not None else NO_HAIRCUT

def _forecast_anchor_date(fridays: List[date], lookback: int) -> date:
    """Return the opening balance date for the first displayed week."""
    if fridays:
        return fridays[0] - timedelta(days=7)
    return _next_friday(date.today()) - timedelta(days=7)


def _resolve_bank_balance(
    client,
    company_id: str,
    db: Session,
    fridays: List[date],
    lookback: int,
    fallback_balance: Optional[float] = None,
) -> tuple[float, date, str, Dict[str, Any]]:
    """Return the opening cash balance and the Friday it reflects."""
    as_of_date = _forecast_anchor_date(fridays, lookback)
    logger.info(
        "[CashForecast] Anchor calendar company=%s generated_on=%s lookback=%s opening_week=%s "
        "first_forecast_week=%s balance_sheet_end_date=%s week_headers=%s",
        company_id,
        date.today().isoformat(),
        lookback,
        fridays[0].isoformat() if fridays else None,
        fridays[lookback].isoformat() if lookback < len(fridays) else None,
        as_of_date.isoformat(),
        [fri.isoformat() for fri in fridays],
    )
    try:
        balance, source, diagnostics = _historical_cash_balance_detail(client, company_id, as_of_date, db)
        diagnostics.update(
            {
                "calendar_generated_on": date.today().isoformat(),
                "opening_balance_week_ending": fridays[0].isoformat() if fridays else None,
                "first_forecast_week_ending": fridays[lookback].isoformat() if lookback < len(fridays) else None,
                "week_headers": [fri.isoformat() for fri in fridays],
                "lookback": lookback,
            }
        )
        return balance, as_of_date, source, diagnostics
    except Exception as exc:
        # PLAN-EXEMPT: 2026-07-09 honest-failure batch (AC-C4) — log the cause redacted
        # (QBO error text can echo tenant data); the failure already surfaces via the raise.
        logger.warning(
            "[CashForecast] Historical cash balance fetch failed for %s as of %s: %s",
            company_id,
            as_of_date,
            redact_to_string(str(exc), max_length=300),
        )
        anchor_error = f"Could not load QBO Balance Sheet Bank Accounts total as of {as_of_date.isoformat()}"
        raise ValueError(anchor_error) from exc


def _compute_reconciliation_delta(
    client,
    company_id: str,
    db: Session,
    opening_balance: float,
    opening_as_of: Optional[date],
    total_in: List[float],
    total_out: List[float],
    lookback: int,
    fridays: "list[date]",
    opening_balance_source: "str | None" = None,
    errors: "list[dict] | None" = None,
) -> "dict | None":
    """Compare balance-sheet cash movement to actuals across the lookback period.

    Returns None when reconciliation is NOT APPLICABLE (no lookback / no anchor). A
    fetch FAILURE also returns None but is logged (redacted) and appended to the
    caller's ``errors`` sink so the served payload's data_quality names the gap —
    never a silently absent reconciliation (AC-C4).
    """
    if client is None or lookback <= 0 or not opening_as_of or not total_in or not total_out:
        return None
    if len(fridays) < lookback:
        return None

    control_as_of = fridays[lookback - 1]
    try:
        control_balance, control_source = _historical_cash_balance(client, company_id, control_as_of, db)
    except Exception as exc:
        # PLAN-EXEMPT: 2026-07-09 honest-failure batch (AC-C4) — a failed control-balance
        # fetch used to vanish (bare return None); it now surfaces in data_quality.
        logger.warning(
            "[CashForecast] Control balance fetch failed for %s as of %s: %s",
            company_id,
            control_as_of,
            redact_to_string(str(exc), max_length=300),
        )
        if errors is not None:
            errors.append(safe_error_detail(
                error_code="cashflow_reconciliation_unavailable",
                user_message=(
                    "The lookback reconciliation could not be computed; the forecast is "
                    "served without the balance-sheet cross-check."
                ),
                exc=exc,
                retryable=True,
                log_context="cashflow_reconciliation_unavailable",
            ))
        return None

    lookback_cols = min(lookback, len(total_in), len(total_out))
    actual_net_change = round(
        sum(float(total_in[idx]) - float(total_out[idx]) for idx in range(lookback_cols)),
        2,
    )
    balance_sheet_change = round(float(control_balance) - float(opening_balance), 2)
    delta = round(balance_sheet_change - actual_net_change, 2)
    return {
        "anchor_as_of": control_as_of.isoformat(),
        "prior_as_of": opening_as_of.isoformat(),
        "anchor_bank_balance": round(float(control_balance), 2),
        "prior_bank_balance": round(float(opening_balance), 2),
        "balance_sheet_change": balance_sheet_change,
        "actuals_net_change": actual_net_change,
        "delta": delta,
        "prior_balance_source": opening_balance_source,
        "anchor_balance_source": control_source,
    }


def _compute_rolling_balances(
    total_in: List[float],
    total_out: List[float],
    starting_balance: float,
    lookback: int,
) -> tuple[List[float], List[float]]:
    """Compute beginning and ending balances from the opening cash anchor."""
    total_cols = len(total_in)
    beginning_balances = [0.0] * total_cols
    ending_balances = [0.0] * total_cols
    if total_cols == 0:
        return beginning_balances, ending_balances

    beginning_balances[0] = round(starting_balance, 2)
    for i in range(total_cols):
        ending_balances[i] = round(beginning_balances[i] + total_in[i] - total_out[i], 2)
        if i + 1 < total_cols:
            beginning_balances[i + 1] = ending_balances[i]
    return beginning_balances, ending_balances


# PLAN-EXEMPT: 2a CFF forecast re-anchor, ratified 2026-06-25 ("proceed with 1 and 2")
def _reanchor_forecast_to_control(
    beginning_balances: list[float],
    ending_balances: list[float],
    total_in: list[float],
    total_out: list[float],
    lookback: int,
    control_cash: float,
) -> tuple[list[float], list[float]]:
    """Re-seed the FORECAST columns (index >= lookback) from the reconciled control cash.

    The opening anchor + lookback actuals roll forward through the HISTORICAL columns
    (< lookback) — that roll surfaces the reconciliation delta and is left untouched. But
    the forward projection must start from the control balance (the actual reconciled bank
    cash as of the last historical week), NOT inherit the actuals-capture drift that left
    every forecast week understated by the reconciliation delta (the 2a defect). No-op unless
    there is at least one historical and one forecast column. Mutates + returns the two lists.
    """
    total_cols = len(total_in)
    if not (0 < lookback < total_cols):
        return beginning_balances, ending_balances
    beginning_balances[lookback] = round(control_cash, 2)
    for i in range(lookback, total_cols):
        ending_balances[i] = round(beginning_balances[i] + total_in[i] - total_out[i], 2)
        if i + 1 < total_cols:
            beginning_balances[i + 1] = ending_balances[i]
    return beginning_balances, ending_balances


def _week_status_summary(db: Session, company_id: str, fridays: List[date]) -> List[Dict[str, Any]]:
    if not fridays:
        return []
    rows = db.query(CashForecastWeekStatus).filter(
        CashForecastWeekStatus.company_id == company_id,
        CashForecastWeekStatus.week_ending.in_(fridays),
    ).all()
    by_week = {_to_date(row.week_ending): row for row in rows}
    summaries: List[Dict[str, Any]] = []
    for week_ending in fridays:
        row = by_week.get(week_ending)
        counts = _actuals_counts_for_week(db, company_id, week_ending)
        if row:
            status = row.status
            pulled = bool(row.pulled_at)
        elif counts["transaction_count"] > 0:
            status = WEEK_STATUS_PENDING if counts["uncategorized_count"] > 0 else WEEK_STATUS_PULLED
            pulled = True
        else:
            status = WEEK_STATUS_NOT_PULLED
            pulled = False
        if counts["uncategorized_count"] > 0 and status in CASCADE_READY_WEEK_STATUSES:
            status = WEEK_STATUS_PENDING
        cascade_ready = (
            status in CASCADE_READY_WEEK_STATUSES
            and counts["uncategorized_count"] == 0
            and (pulled or counts["transaction_count"] > 0)
        )
        summaries.append({
            "week_ending": week_ending.isoformat(),
            "status": status,
            "transaction_count": counts["transaction_count"],
            "categorized_count": counts["categorized_count"],
            "uncategorized_count": counts["uncategorized_count"],
            "excluded_count": counts["excluded_count"],
            "decomposed_count": counts["decomposed_count"],
            "cascade_ready": cascade_ready,
            "pulled_at": row.pulled_at.isoformat() if row and row.pulled_at else None,
            "reviewed_at": row.reviewed_at.isoformat() if row and row.reviewed_at else None,
            "reconciled_at": row.reconciled_at.isoformat() if row and row.reconciled_at else None,
        })
    return summaries


# Page bound per QBO entity for the 180-day cash history (1000 rows/page). A busier
# history is read up to the bound and reported partial, never silently cut.
HISTORY_MAX_PAGES = 50

_HISTORY_DIRECTION = {"Deposit": "cash-in", "Payment": "cash-in", "Purchase": "cash-out", "BillPayment": "cash-out"}


def _history_rows(client, entity: str, since: date, errors: "list[dict] | None") -> "list[dict]":
    """Every ``entity`` row since ``since`` (paginated); a page-bound stop lands in ``errors``."""
    rows, truncated = query_all_pages(
        client, entity, txn_date_where(since.isoformat()), max_pages=HISTORY_MAX_PAGES,
    )
    if truncated:
        logger.warning(
            "[CashForecast] %s history exceeded %s pages; patterns use a partial history",
            entity, HISTORY_MAX_PAGES,
        )
        if errors is not None:
            errors.append(safe_error_detail(
                error_code=f"cashflow_{entity.lower()}_history_truncated",
                user_message=(
                    f"Historical {entity} activity is larger than the read limit; recurring "
                    f"{_HISTORY_DIRECTION[entity]} patterns use a partial history."
                ),
                retryable=False,
            ))
    return rows


# PLAN-EXEMPT: 2026-07-09 honest-failure batch (AC-C4) — a QBO query failure in the history
# fetchers used to degrade to an EMPTY history (run-rate patterns silently missing from the
# forecast, a success-shaped lie). Each failure now also lands in the caller's ``errors``
# sink so the payload's data_quality names the gap.
def _fetch_deposits(client, since: date, errors: "list[dict] | None" = None) -> "list[dict]":
    """Fetch Deposit + Payment transactions (customer cash-in) for the last N months.

    Dedup strategy: Deposits may contain LinkedTxn references to Payment
    records.  We collect those IDs and skip corresponding Payments below
    so each dollar is counted exactly once.
    """
    results = []
    # Track Payment IDs that are linked inside Deposits (same approach as actuals pull)
    deposit_linked_payment_ids: set = set()

    # ── Deposits ─────────────────────────────────────────────────────────
    try:
        for d in _history_rows(client, "Deposit", since, errors):
            total = float(d.get("TotalAmt", 0) or 0)
            txn_date_raw = d.get("TxnDate", "")
            try:
                txn_date = date.fromisoformat(txn_date_raw)
            except ValueError:
                continue

            lines = d.get("Line", [])
            non_payment_total = 0.0
            non_payment_name = "Other Cash In"
            has_linked_payments = False

            for line in lines:
                linked_txns = line.get("LinkedTxn", [])
                is_payment_linked = any(
                    lt.get("TxnType") in ("Payment", "SalesReceipt") for lt in linked_txns
                )
                if is_payment_linked:
                    has_linked_payments = True
                    # Collect linked Payment IDs so we can skip them below
                    for lt in linked_txns:
                        if lt.get("TxnType") in ("Payment", "SalesReceipt"):
                            deposit_linked_payment_ids.add(lt.get("TxnId", ""))
                    continue  # skip — will be counted via Deposit total

                line_amt = float(line.get("Amount", 0) or 0)
                non_payment_total += line_amt

                # Try to get entity name from the non-payment line
                detail = line.get("DepositLineDetail") or {}
                entity = detail.get("Entity") or {}
                entity_ref = entity.get("EntityRef") or {}
                if entity_ref.get("name"):
                    non_payment_name = entity_ref["name"]
                elif entity.get("name"):
                    non_payment_name = entity["name"]

            if has_linked_payments and non_payment_total <= 0:
                # Entire deposit is Payment-linked — use the Deposit total
                # with entity name from the linked payment lines
                # Find entity from linked lines
                dep_name = "Other Cash In"
                for line in lines:
                    linked_txns = line.get("LinkedTxn", [])
                    if linked_txns:
                        detail = line.get("DepositLineDetail") or {}
                        entity = detail.get("Entity") or {}
                        entity_ref = entity.get("EntityRef") or {}
                        if entity_ref.get("name"):
                            dep_name = entity_ref["name"]
                            break
                        if entity.get("name"):
                            dep_name = entity["name"]
                            break
                results.append({"name": dep_name, "txn_date": txn_date, "amount": abs(total)})
            elif has_linked_payments:
                # Mixed deposit: Deposit total counts the linked portion;
                # we also add the non-payment portion separately
                results.append({"name": non_payment_name, "txn_date": txn_date, "amount": abs(total)})
            else:
                # Pure deposit (no linked payments) — find customer from lines
                name = "Other Cash In"
                for line in lines:
                    detail = line.get("DepositLineDetail") or {}
                    entity = detail.get("Entity") or {}
                    entity_ref = entity.get("EntityRef") or {}
                    if entity_ref.get("name"):
                        name = entity_ref["name"]
                        break
                    if entity.get("name"):
                        name = entity["name"]
                        break
                results.append({"name": name, "txn_date": txn_date, "amount": total})
    except Exception as e:
        # PLAN-EXEMPT: 2026-07-09 honest-failure batch (AC-C4) — surfaced, not swallowed.
        logger.warning("[CashForecast] Deposit fetch failed: %s", redact_to_string(str(e), max_length=500))
        if errors is not None:
            errors.append(safe_error_detail(
                error_code="cashflow_deposit_history_unavailable",
                user_message="Historical deposit activity could not be loaded; recurring cash-in patterns may be incomplete.",
                exc=e,
                retryable=True,
                log_context="cashflow_deposit_history_unavailable",
            ))

    logger.info(f"[CashForecast] Historical: {len(deposit_linked_payment_ids)} Payment IDs linked to Deposits (will skip)")

    # ── Payments (AR collections — most customer cash comes in here) ─────
    # Skip any Payments already captured by a Deposit above.
    try:
        for p in _history_rows(client, "Payment", since, errors):
            pmt_id = p.get("Id", "")
            if pmt_id in deposit_linked_payment_ids:
                continue  # already counted via the Deposit

            total = float(p.get("TotalAmt", 0) or 0)
            txn_date_raw = p.get("TxnDate", "")
            try:
                txn_date = date.fromisoformat(txn_date_raw)
            except ValueError:
                continue

            # Payment always has CustomerRef at the top level
            customer_ref = p.get("CustomerRef") or {}
            name = customer_ref.get("name", "Other Cash In")

            results.append({"name": name, "txn_date": txn_date, "amount": total})
    except Exception as e:
        # PLAN-EXEMPT: 2026-07-09 honest-failure batch (AC-C4) — surfaced, not swallowed.
        logger.warning("[CashForecast] Payment fetch failed: %s", redact_to_string(str(e), max_length=500))
        if errors is not None:
            errors.append(safe_error_detail(
                error_code="cashflow_payment_history_unavailable",
                user_message="Historical customer payment activity could not be loaded; recurring cash-in patterns may be incomplete.",
                exc=e,
                retryable=True,
                log_context="cashflow_payment_history_unavailable",
            ))

    return results


# PLAN-EXEMPT: 2026-07-09 honest-failure batch (AC-C4) — same errors-sink contract as
# _fetch_deposits above: a query failure surfaces in data_quality, never an empty success.
def _fetch_outflows(client, since: date, errors: "list[dict] | None" = None) -> "list[dict]":
    """Fetch Purchase + BillPayment transactions (vendor cash-out).

    Filters out Bill.com clearing account transactions which are
    pass-through entries, not real cash movement.
    """
    results = []

    # Purchases (checks, ACH, credit card payments, etc.)
    try:
        for p in _history_rows(client, "Purchase", since, errors):
            total = float(p.get("TotalAmt", 0) or 0)
            txn_date_raw = p.get("TxnDate", "")
            try:
                txn_date = date.fromisoformat(txn_date_raw)
            except ValueError:
                continue

            # Bug 6: Skip Bill.com clearing account transactions
            acct_name = (p.get("AccountRef") or {}).get("name", "")
            if "bill.com" in acct_name.lower() and "clearing" in acct_name.lower():
                continue

            entity_ref = p.get("EntityRef") or {}
            name = entity_ref.get("name", "Non Routine Expenses")

            results.append({"name": name, "txn_date": txn_date, "amount": total})
    except Exception as e:
        # PLAN-EXEMPT: 2026-07-09 honest-failure batch (AC-C4) — surfaced, not swallowed.
        logger.warning("[CashForecast] Purchase fetch failed: %s", redact_to_string(str(e), max_length=500))
        if errors is not None:
            errors.append(safe_error_detail(
                error_code="cashflow_purchase_history_unavailable",
                user_message="Historical purchase activity could not be loaded; recurring cash-out patterns may be incomplete.",
                exc=e,
                retryable=True,
                log_context="cashflow_purchase_history_unavailable",
            ))

    # Bill Payments
    try:
        for bp in _history_rows(client, "BillPayment", since, errors):
            total = float(bp.get("TotalAmt", 0) or 0)
            txn_date_raw = bp.get("TxnDate", "")
            try:
                txn_date = date.fromisoformat(txn_date_raw)
            except ValueError:
                continue

            vendor_ref = bp.get("VendorRef") or {}
            name = vendor_ref.get("name", "Non Routine Expenses")

            results.append({"name": name, "txn_date": txn_date, "amount": total})
    except Exception as e:
        # PLAN-EXEMPT: 2026-07-09 honest-failure batch (AC-C4) — surfaced, not swallowed.
        logger.warning("[CashForecast] BillPayment fetch failed: %s", redact_to_string(str(e), max_length=500))
        if errors is not None:
            errors.append(safe_error_detail(
                error_code="cashflow_billpayment_history_unavailable",
                user_message="Historical bill-payment activity could not be loaded; recurring cash-out patterns may be incomplete.",
                exc=e,
                retryable=True,
                log_context="cashflow_billpayment_history_unavailable",
            ))

    return results


def _fetch_open_invoices(client, collection_haircuts=None) -> list[dict]:
    """Fetch all open (unpaid/partially paid) invoices from QBO.

    Extracts the payment terms (Net 30, Net 60, etc.) from the gap between
    TxnDate and DueDate, which tells us when the customer is EXPECTED to pay.
    Paginates through all results to ensure completeness.

    ``collection_haircuts`` is the PER-TENANT bucket curve from
    derive_collection_haircuts (AC-3); None/thin buckets ⇒ face value, labeled.
    """
    haircut_buckets = (collection_haircuts or {}).get("buckets") or None
    results = []
    try:
        start_pos = 1
        while True:
            resp = client.query(
                f"SELECT * FROM Invoice WHERE Balance > '0' STARTPOSITION {start_pos} MAXRESULTS 1000"
            )
            batch = resp.get("QueryResponse", {}).get("Invoice", [])
            if not batch:
                break
            for inv in batch:
                balance = float(inv.get("Balance", 0) or 0)
                if balance <= 0:
                    continue
                customer_ref = inv.get("CustomerRef") or {}
                customer = customer_ref.get("name", "Other Cash In")
                due_date_str = inv.get("DueDate", "")
                txn_date_str = inv.get("TxnDate", "")
                try:
                    due_date = date.fromisoformat(due_date_str) if due_date_str else None
                except ValueError:
                    due_date = None
                try:
                    txn_date = date.fromisoformat(txn_date_str) if txn_date_str else None
                except ValueError:
                    txn_date = None

                # Compute payment terms from the gap between invoice date and due date
                # e.g., invoice dated Jan 1 with due date Jan 31 = Net 30
                terms_days = 30  # default if we can't compute
                if txn_date and due_date:
                    terms_days = max(0, (due_date - txn_date).days)

                # Compute days past due for haircut calculation
                today = date.today()
                days_past_due = (today - due_date).days if due_date and due_date < today else 0

                results.append({
                    "customer": customer,
                    "invoice_id": inv.get("Id"),
                    "doc_number": inv.get("DocNumber", ""),
                    "due_date": due_date,
                    "txn_date": txn_date,
                    "balance": balance,
                    "terms_days": terms_days,
                    "days_past_due": max(0, days_past_due),
                    "collection_pct": _collection_haircut(days_past_due, haircut_buckets),
                })
            if len(batch) < 1000:
                break
            start_pos += 1000
    except Exception as e:
        logger.warning("[CashForecast] Open invoice fetch failed: %s", redact_to_string(str(e), max_length=500))
        raise RuntimeError("Open invoice data could not be loaded from QuickBooks.") from e
    logger.info(f"[CashForecast] Fetched {len(results)} open invoices (total AR: ${sum(r['balance'] for r in results):,.2f})")
    return results


def _fetch_open_bills(client) -> List[Dict]:
    """Fetch all open (unpaid/partially paid) bills from QBO.
    
    Extracts the payment terms from the gap between TxnDate and DueDate.
    Paginates through all results to ensure completeness.
    """
    results = []
    try:
        start_pos = 1
        while True:
            resp = client.query(
                f"SELECT * FROM Bill WHERE Balance > '0' STARTPOSITION {start_pos} MAXRESULTS 1000"
            )
            batch = resp.get("QueryResponse", {}).get("Bill", [])
            if not batch:
                break
            for bill in batch:
                balance = float(bill.get("Balance", 0) or 0)
                if balance <= 0:
                    continue
                vendor_ref = bill.get("VendorRef") or {}
                vendor = vendor_ref.get("name", "Non Routine Expenses")
                due_date_str = bill.get("DueDate", "")
                txn_date_str = bill.get("TxnDate", "")
                try:
                    due_date = date.fromisoformat(due_date_str) if due_date_str else None
                except ValueError:
                    due_date = None
                try:
                    txn_date = date.fromisoformat(txn_date_str) if txn_date_str else None
                except ValueError:
                    txn_date = None

                # Compute payment terms from the gap between bill date and due date
                terms_days = 30  # default if we can't compute
                if txn_date and due_date:
                    terms_days = max(0, (due_date - txn_date).days)

                results.append({
                    "vendor": vendor,
                    "bill_id": bill.get("Id"),
                    "doc_number": bill.get("DocNumber", ""),
                    "due_date": due_date,
                    "txn_date": txn_date,
                    "balance": balance,
                    "terms_days": terms_days,
                })
            if len(batch) < 1000:
                break
            start_pos += 1000
    except Exception as e:
        logger.warning("[CashForecast] Open bill fetch failed: %s", redact_to_string(str(e), max_length=500))
        raise RuntimeError("Open bill data could not be loaded from QuickBooks.") from e
    logger.info(f"[CashForecast] Fetched {len(results)} open bills (total AP: ${sum(r['balance'] for r in results):,.2f})")
    return results


# ─────────────────────────────────────────────────────────────────────────────
# Main Endpoint
# ─────────────────────────────────────────────────────────────────────────────

BALANCE_OVERRIDE_TYPE = "balance"  # entity_type for beginning-balance overrides

# PLAN-EXEMPT: ratified plan 20260702-qa-remediation W-2 (F-3). A served snapshot older than
# this is flagged "stale" in the payload (still served — never a blocking in-request rebuild).
SNAPSHOT_STALE_SECONDS = 24 * 3600

# PLAN-EXEMPT: ratified plan 20260702-qa-remediation W-2 (F-3 week-filtered override query).
# Every config sentinel (BALANCE 9995-12-31 … AVG 9999-12-31) sorts above this floor, so ONE
# range predicate keeps all sentinel rows while cell overrides are filtered to the visible window.
_SENTINEL_FLOOR = date(9995, 1, 1)


def _load_window_overrides(db: Session, company_id: str, fridays: List[date]) -> List[CashForecastOverride]:
    """The company's overrides RELEVANT to this forecast window (F-3): cell overrides for the
    displayed Fridays, ALL config sentinels (day/avg/hidden/order/balance — week_ending ≥ the
    sentinel floor), and legacy beginning-balance rows (entity_type='balance', which historically
    carried arbitrary real dates). Replaces the unfiltered full-table read; pairs with the
    (company_id, week_ending) composite index so the read is index-served."""
    from sqlalchemy import or_

    return db.query(CashForecastOverride).filter(
        CashForecastOverride.company_id == company_id,
        or_(
            CashForecastOverride.week_ending.in_(fridays),
            CashForecastOverride.week_ending >= _SENTINEL_FLOOR,
            CashForecastOverride.entity_type == BALANCE_OVERRIDE_TYPE,
        ),
    ).all()


@router.get("/weekly")
def get_weekly_forecast(
    company_id: str = Query(...),
    weeks: int = Query(13, ge=4, le=26),
    lookback: int = Query(1, ge=0, le=4),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
) -> Dict[str, Any]:
    """
    Returns a rolling N-week cash flow forecast.

    Reads from a cached DB snapshot (fast) and applies user overrides,
    actuals, and balance adjustments on top.

    W-2 F-3 (plan 20260702-qa-remediation): a snapshot MISS no longer runs the full synchronous
    QBO build in-request (the live-confirmed multi-second stall). It enqueues the background
    refresh task and returns an honest zero-grid payload flagged ``snapshot_state="building"``;
    the nightly beat + POST /weekly/refresh keep existing snapshots warm.
    """
    check_tenant_access("VIEWER", company_id=company_id, db=db, user=current_user)
    return _build_weekly_forecast_payload(
        company_id=company_id, weeks=weeks, lookback=lookback, db=db, allow_sync_build=False,
    )


def _enqueue_snapshot_refresh(company_id: str, weeks: int, lookback: int) -> Optional[Dict[str, Any]]:
    """Enqueue the background snapshot rebuild (F-3). Returns a data-quality error dict when the
    enqueue itself fails (broker down) so the degraded payload surfaces it instead of a 500."""
    try:
        from ..tasks.cash_forecast_refresh import refresh_realm_forecast_snapshot

        refresh_realm_forecast_snapshot.delay(
            qbo_realm_id=company_id, weeks=weeks, lookback=lookback,
        )
        return None
    except Exception as exc:  # noqa: BLE001 — a broker outage must not 500 a read endpoint
        logger.error("[CashForecast] snapshot refresh enqueue failed for %s: %s",
                     company_id, redact_to_string(str(exc), max_length=300))
        return safe_error_detail(
            error_code="cashflow_refresh_enqueue_failed",
            user_message="The forecast rebuild could not be scheduled. Use Refresh from QBO to build it now.",
            exc=exc,
            retryable=True,
            log_context="cashflow_refresh_enqueue_failed",
        )


# PLAN-EXEMPT: 2026-07-09 honest-failure batch (AC-C2) — ``unmatched`` is computed from the
# local DB (no QBO needed) even while the snapshot builds, so the honest-gap ledger is never
# zeroed just because the grid is empty; ``beyond_horizon`` is None (unknown until built).
def _snapshot_building_payload(
    company_id: str,
    weeks: int,
    lookback: int,
    enqueue_error: "dict | None" = None,
    db: "Session | None" = None,
) -> "dict[str, Any]":  # CRAFT-OK: dynamic forecast payload boundary — the /weekly response is a provider-shaped grid dict
    """The full /weekly payload SHAPE with an empty grid + ``snapshot_state="building"`` — served
    on a snapshot miss while the background build runs. Every key the snapshot-served payload
    carries is present (zeros/empties), so the page renders and the data_quality envelope tells
    the truth; nothing is fabricated."""
    fridays = _week_fridays(weeks, lookback=lookback)
    total_cols = len(fridays)
    zeros = [0.0] * total_cols
    errors: List[Dict[str, Any]] = [safe_error_detail(
        error_code="cashflow_snapshot_building",
        user_message=(
            "This company's cash flow forecast is being built from QuickBooks in the background. "
            "Reload in a moment, or use Refresh from QBO to build it now."
        ),
        retryable=True,
        log_context="cashflow_snapshot_building",
    )]
    if enqueue_error is not None:
        errors.append(enqueue_error)
    return {
        "week_headers": [f.isoformat() for f in fridays],
        "lookback": lookback,
        "beginning_balances": list(zeros),
        "opening_balance_week_ending": fridays[0].isoformat() if fridays else None,
        "first_forecast_week_ending": fridays[lookback].isoformat() if lookback < len(fridays) else None,
        "customers": [],
        "other_cash_in": list(zeros),
        "total_cash_in": list(zeros),
        "vendors": [],
        "other_cash_out": list(zeros),
        "total_cash_out": list(zeros),
        "ending_balances": list(zeros),
        "bank_balance": 0.0,
        "bank_balance_as_of": None,
        "bank_balance_source": "snapshot_building",
        "anchor_diagnostics": None,
        "reconciliation_delta": None,
        "forecast_reanchored_to_control": False,
        "beginning_balance_override": None,
        "uncategorized_actuals": 0,
        "week_statuses": [],
        "review_required_weeks": [],
        "past_due_in": 0.0,
        "past_due_out": 0.0,
        "errors": errors,
        "data_quality": {
            "is_partial": True,
            "unavailable_sections": [err["error_code"] for err in errors if isinstance(err, dict)],
        },
        "collection_haircuts": None,
        # PLAN-EXEMPT: 2026-07-09 honest-failure batch (AC-C2) — honest-gap ledger present
        # even while building: unmatched comes from the local DB, beyond_horizon is unknown.
        "unmatched": _building_unmatched_block(db, company_id),
        "beyond_horizon": None,
        "non_recurring_history": None,
        "liquidity": None,
        "snapshot_age_seconds": None,
        "snapshot_state": "building",
        "snapshot_version": None,
        "snapshot_computed_at": None,
        "export_id": None,
        "export_company_id": company_id,
        "export_schema_version": 1,
        "export_capture_state": "building",
        "export_error": None,
    }


def _building_unmatched_block(db: "Session | None", company_id: str) -> dict:
    # PLAN-EXEMPT: 2026-07-09 honest-failure batch (AC-C2) — the unmapped-entity detection
    # is a local-DB read (cff_integrity), so the building payload can still report the
    # honest gap instead of a fabricated zero.
    unmapped_entities = list_unmapped_categorized_entities(db, company_id) if db is not None else []
    reasons: "dict[str, dict]" = {}
    if unmapped_entities:
        reasons["no_forecast_row"] = {
            "count": len(unmapped_entities),
            "total_amount": round(sum(e["total_amount"] for e in unmapped_entities), 2),
        }
    return {
        "count": len(unmapped_entities),
        "reasons": reasons,
        "entities": unmapped_entities,
    }


def _freeze_version_best_effort(db: Session, company_id: str, payload: dict) -> dict:
    """Freeze the served payload as this week's snapshot version (AC-2B.1 accuracy
    provenance). A freeze failure must never fail the serve/refresh path — it is
    logged (redacted) AND surfaced in the returned summary, not silently dropped."""
    try:
        return freeze_snapshot_version(db, company_id, payload)
    except Exception as exc:  # noqa: BLE001 — surfaced in the payload + logged; the serve path must not 500 on provenance capture
        logger.error("[CashForecast] snapshot version freeze failed for %s: %s",
                     company_id, redact_to_string(str(exc), max_length=300))
        db.rollback()
        return {"version_frozen": False, "reason": "freeze_failed"}


def _attach_export_snapshot_best_effort(
    db: Session,
    company_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Attach one durable exact-export identity without risking the grid serve.

    The isolated capture service owns its own transaction.  A capture failure is
    therefore export-local: the finalized weekly payload remains usable and names
    the retryable unavailable state instead of fabricating an ID or rolling back
    the caller's work.
    """
    payload.update(
        {
            "export_id": None,
            "export_company_id": company_id,
            "export_schema_version": FORECAST_EXPORT_SCHEMA_VERSION,
            "export_capture_state": "unavailable",
            "export_error": None,
        }
    )
    if payload.get("snapshot_state") == "building":
        payload["export_capture_state"] = "building"
        return payload

    try:
        organization = organization_for_company(db, company_id)
        if organization is None:
            raise PermissionError("company is not mapped to an organization")
        company_display_name = resolve_company_display_name(
            db,
            organization_id=organization.id,
            company_id=company_id,
        )
        content = content_from_weekly_payload(
            payload,
            company_id=company_id,
            company_display_name=company_display_name,
        )
        export = capture_forecast_export(
            content,
            organization_id=organization.id,
        )
    except Exception as exc:  # noqa: BLE001 — export failure must not block the forecast grid
        payload["export_error"] = safe_error_detail(
            error_code="cashflow_export_capture_failed",
            user_message=(
                "The forecast is available, but its downloadable snapshot could not be "
                "prepared. Retry the download after the next forecast refresh."
            ),
            exc=exc,
            retryable=True,
            log_context="cashflow_export_capture_failed",
        )
        return payload

    payload["export_id"] = str(export.export_id)
    payload["export_capture_state"] = "ready"
    return payload


def _build_weekly_forecast_payload(
    *,
    company_id: str,
    weeks: int,
    lookback: int,
    db: Session,
    allow_sync_build: bool = True,
) -> dict:
    # Check for cached snapshot
    snapshot = db.query(CashForecastSnapshot).filter(
        CashForecastSnapshot.company_id == company_id,
    ).first()

    # F-3: the interactive API path (allow_sync_build=False) never runs the full QBO build
    # in-request — it enqueues the background rebuild and serves the honest building payload.
    # Programmatic callers (reports, assistant) keep the synchronous build: they already run in
    # background contexts where a blocking QBO fetch is correct and an empty grid is not.
    if not (snapshot and snapshot.snapshot_data) and not allow_sync_build:
        logger.info("[CashForecast] No snapshot for %s — enqueueing background build", company_id)
        enqueue_error = _enqueue_snapshot_refresh(company_id, weeks, lookback)
        # PLAN-EXEMPT: 2026-07-09 honest-failure batch (AC-C2) — db passed so the building
        # payload carries the real unmatched ledger (a local read), not a fabricated zero.
        return _snapshot_building_payload(company_id, weeks, lookback, enqueue_error, db=db)

    if snapshot and snapshot.snapshot_data:
        logger.info(f"[CashForecast] Using cached snapshot for {company_id} "
                     f"(computed {snapshot.computed_at})")
        snapshot_data = snapshot.snapshot_data
        try:
            client = get_qbo_client_from_db(db, company_id)
            fridays = _week_fridays(weeks, lookback=lookback)
            bank_balance, bank_balance_as_of, bank_balance_source, anchor_diagnostics = _resolve_bank_balance(
                client,
                company_id,
                db,
                fridays,
                lookback,
                fallback_balance=float(snapshot.qbo_bank_balance or 0),
            )
        except Exception as exc:
            raise user_safe_http_exception(
                status_code=502,
                error_code="cashflow_anchor_balance_unavailable",
                user_message="Could not load the QuickBooks bank balance anchor for this forecast.",
                exc=exc,
                retryable=True,
                log_context="cashflow_anchor_balance_unavailable",
            ) from exc
        
        computed_at = snapshot.computed_at
        if computed_at and computed_at.tzinfo is None:
            computed_at = computed_at.replace(tzinfo=timezone.utc)
            
        snapshot_age_seconds = (
            _datetime.now(timezone.utc) - computed_at
        ).total_seconds() if computed_at else None
    else:
        # No snapshot — do initial full QBO build and cache it
        logger.info(f"[CashForecast] No snapshot for {company_id} — full QBO fetch")
        snapshot_data, bank_balance, bank_balance_as_of, bank_balance_source, anchor_diagnostics = _build_forecast_from_qbo(company_id, weeks, lookback, db)
        computed_at = _save_snapshot(db, company_id, snapshot_data, bank_balance)
        snapshot_age_seconds = 0

    # Apply live layer (overrides, actuals, rolling balance)
    result = _apply_live_layer(
        snapshot_data,
        bank_balance,
        company_id,
        weeks,
        lookback,
        db,
        bank_balance_as_of=bank_balance_as_of,
        bank_balance_source=bank_balance_source,
        anchor_diagnostics=anchor_diagnostics,
    )
    result["snapshot_age_seconds"] = snapshot_age_seconds
    result["snapshot_computed_at"] = computed_at.isoformat() if computed_at else None
    # F-3 stale-with-flag: the payload names its own freshness so the UI can offer a refresh
    # instead of the backend blocking the request on QBO.
    result["snapshot_state"] = (
        "ready"
        if snapshot_age_seconds is not None and snapshot_age_seconds <= SNAPSHOT_STALE_SECONDS
        else "stale"
    )
    # A fresh sync build just replaced the serve snapshot — freeze this week's frozen
    # version alongside it (forecast-as-made provenance for accuracy scoring; the key
    # is always present so the payload shape is stable).
    result["snapshot_version"] = (
        _freeze_version_best_effort(db, company_id, result)
        if snapshot_age_seconds == 0
        else None
    )
    return _attach_export_snapshot_best_effort(db, company_id, result)


def build_weekly_forecast(
    company_id: str,
    weeks: int = 13,
    lookback: int = 1,
    db: Optional[Session] = None,
) -> Dict[str, Any]:
    """Programmatic entry point for the same forecast payload returned by the API."""
    if db is None:
        raise ValueError("db session is required")
    return _build_weekly_forecast_payload(company_id=company_id, weeks=weeks, lookback=lookback, db=db)


@router.post("/weekly/refresh")
def refresh_weekly_forecast(
    company_id: str = Query(...),
    weeks: int = Query(13, ge=4, le=26),
    lookback: int = Query(1, ge=0, le=4),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
) -> Dict[str, Any]:
    """
    Force a full QBO refresh: fetches all invoices, bills, DSO/DPO,
    historical patterns, and saves a new snapshot.  Returns the fresh
    forecast with overrides and actuals applied.
    """
    check_tenant_access("EDITOR", company_id=company_id, db=db, user=current_user)
    logger.info(f"[CashForecast] Full QBO refresh requested for {company_id}")
    snapshot_data, bank_balance, bank_balance_as_of, bank_balance_source, anchor_diagnostics = _build_forecast_from_qbo(company_id, weeks, lookback, db)
    computed_at = _save_snapshot(db, company_id, snapshot_data, bank_balance)

    result = _apply_live_layer(
        snapshot_data,
        bank_balance,
        company_id,
        weeks,
        lookback,
        db,
        bank_balance_as_of=bank_balance_as_of,
        bank_balance_source=bank_balance_source,
        anchor_diagnostics=anchor_diagnostics,
    )
    result["snapshot_age_seconds"] = 0
    result["snapshot_state"] = "ready"
    result["snapshot_computed_at"] = computed_at.isoformat()
    # Freeze the refreshed grid as this week's version (accuracy provenance, AC-2B.1).
    result["snapshot_version"] = _freeze_version_best_effort(db, company_id, result)
    return _attach_export_snapshot_best_effort(db, company_id, result)


# ─────────────────────────────────────────────────────────────────────────────
# Read-only insight endpoints — scenarios · variance · accuracy (AC-2B.1 build-new)
# ─────────────────────────────────────────────────────────────────────────────


@router.get("/weekly/scenarios", dependencies=[Depends(global_tenant_guard)])
def get_weekly_forecast_scenarios(
    company_id: str = Query(...),
    weeks: int = Query(13, ge=4, le=26),
    lookback: int = Query(1, ge=0, le=4),
    min_cash_floor: float = Query(0.0, description="Liquidity floor (e.g. a covenant minimum); default zero"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
) -> dict:  # LAYER-OK: dynamic forecast-grid passthrough mirroring the existing /weekly contract
    """Named TWCF stress cases as READ-ONLY transforms over the served snapshot.

    Every case re-anchors to the SAME bank balance as the base grid; user overrides
    are already baked into the base (overrides first, then the stress transform);
    NOTHING is persisted — a forecast change still goes through the approval queue.
    """
    check_tenant_access("VIEWER", company_id=company_id, db=db, user=current_user)
    base = _build_weekly_forecast_payload(
        company_id=company_id, weeks=weeks, lookback=lookback, db=db, allow_sync_build=False,
    )
    if base.get("snapshot_state") == "building":
        return {"snapshot_state": "building", "base": None, "cases": [],
                "errors": base.get("errors") or []}
    return {
        "snapshot_state": base.get("snapshot_state"),
        "snapshot_age_seconds": base.get("snapshot_age_seconds"),
        "writeback": "read_only",
        "base": {
            "week_headers": base.get("week_headers"),
            "lookback": base.get("lookback"),
            "total_cash_in": base.get("total_cash_in"),
            "total_cash_out": base.get("total_cash_out"),
            "beginning_balances": base.get("beginning_balances"),
            "ending_balances": base.get("ending_balances"),
            "liquidity": liquidity_from_payload(base, min_cash_floor=min_cash_floor),
        },
        "cases": build_scenario_set(base, min_cash_floor=min_cash_floor),
    }


@router.get("/weekly/variance", dependencies=[Depends(global_tenant_guard)])
def get_weekly_forecast_variance(
    company_id: str = Query(...),
    week_ending: str | None = Query(None, description="Target week Friday (ISO); default = last completed week"),
    horizon: int = Query(1, ge=0, le=13, description="Weeks between forecast capture and the target week"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
) -> dict:  # LAYER-OK: dynamic forecast-grid passthrough mirroring the existing /weekly contract
    """Per-entity variance for one week vs the FROZEN forecast made ``horizon`` weeks
    earlier (timing / systematic / one-off taxonomy). Scoring against the re-anchored
    serve grid would be tautological — only frozen versions are scored."""
    check_tenant_access("VIEWER", company_id=company_id, db=db, user=current_user)
    if week_ending:
        target = date.fromisoformat(week_ending[:10])
    else:
        target = _next_friday(date.today()) - timedelta(weeks=1)
    version = version_for_target_week(db, company_id, target, horizon_weeks=horizon)
    if version is None:
        return {
            "status": "no_frozen_version",
            "target_week_ending": target.isoformat(),
            "horizon_weeks": horizon,
            "detail": (
                "No frozen forecast version exists for this week/horizon yet — versions "
                "are captured by the nightly refresh going forward; history cannot be "
                "reconstructed retroactively."
            ),
        }
    actuals = actuals_by_entity_for_weeks(db, company_id, [target, target + timedelta(weeks=1)])
    report = build_week_variance_report(
        version.grid or {},
        target.isoformat(),
        actuals.get(target, {}),
        next_week_actuals_by_entity=actuals.get(target + timedelta(weeks=1), {}),
    )
    report["horizon_weeks"] = horizon
    report["as_of_week_ending"] = version.as_of_week_ending.isoformat()
    return report


@router.get("/weekly/accuracy", dependencies=[Depends(global_tenant_guard)])
def get_weekly_forecast_accuracy(
    company_id: str = Query(...),
    horizon: int = Query(1, ge=0, le=13, description="Weeks between forecast capture and each scored week"),
    max_weeks: int = Query(13, ge=1, le=52),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
) -> dict:  # LAYER-OK: dynamic forecast-grid passthrough mirroring the existing /weekly contract
    """Forecast-accuracy tracking: MAPE (magnitude) + bias (direction) + MAD ($) over
    FROZEN weekly versions, ``pull_partial`` weeks excluded-and-counted. Method only —
    no numeric accuracy-threshold judgment is encoded (gap G5, refuse over fabricate)."""
    check_tenant_access("VIEWER", company_id=company_id, db=db, user=current_user)
    scored = collect_scored_weeks(db, company_id, horizon_weeks=horizon, max_weeks=max_weeks)
    result = compute_forecast_accuracy(scored)
    result["horizon_weeks"] = horizon
    result["scored_weeks"] = scored
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Snapshot Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _save_snapshot(
    db: Session,
    company_id: str,
    snapshot_data: Dict,
    bank_balance: float,
) -> _datetime:
    """Upsert a serve snapshot and return its durable computation timestamp."""
    computed_at = _datetime.now(timezone.utc)
    existing = db.query(CashForecastSnapshot).filter(
        CashForecastSnapshot.company_id == company_id,
    ).first()
    if existing:
        existing.snapshot_data = snapshot_data   # type: ignore[assignment]
        existing.qbo_bank_balance = bank_balance # type: ignore[assignment]
        existing.computed_at = computed_at        # type: ignore[assignment]
    else:
        existing = CashForecastSnapshot(
            company_id=company_id,
            snapshot_data=snapshot_data,
            qbo_bank_balance=bank_balance,
            computed_at=computed_at,
        )
        db.add(existing)
    db.commit()
    return computed_at


def _build_forecast_from_qbo(
    company_id: str, weeks: int, lookback: int, db: Session,
) -> tuple:
    """
    Full QBO fetch + projection.

    Returns (snapshot_data, bank_balance, bank_balance_as_of, bank_balance_source, anchor_diagnostics).

    snapshot_data contains the raw entity rows before overrides/actuals:
      { customer_rows, vendor_rows, week_headers, lookback }
    """
    try:
        client = get_qbo_client_from_db(db, company_id)
    except Exception as e:
        raise user_safe_http_exception(
            status_code=502,
            error_code="qbo_connection_failed",
            user_message="Could not connect to QuickBooks for this company.",
            exc=e,
            retryable=True,
            log_context="cashflow_forecast_qbo_connection_failed",
        )

    total_cols = weeks + lookback  # noqa: F841
    fridays = _week_fridays(weeks, lookback=lookback)
    since = date.today() - timedelta(days=180)

    # ── 1. Invoice-driven data (primary source) ─────────────────────────
    # Per-tenant collection haircuts (AC-3): derived from this tenant's Payment↔Invoice
    # history — never a universal constant. On failure/thin history the forecast
    # proceeds at FACE VALUE with the basis labeled (an honest unhaircut, not 0.85).
    haircut_error: dict | None = None
    try:
        collection_haircuts = derive_collection_haircuts(client)
    except Exception as exc:
        haircut_error = safe_error_detail(
            error_code="cashflow_collection_haircuts_unavailable",
            user_message=(
                "Historical collection rates could not be derived; open receivables "
                "are carried at face value."
            ),
            exc=exc,
            retryable=True,
            log_context="cashflow_collection_haircuts_unavailable",
        )
        collection_haircuts = {"buckets": None, "basis": "insufficient_history"}
    try:
        open_invoices = _fetch_open_invoices(client, collection_haircuts)
    except Exception as exc:
        raise user_safe_http_exception(
            status_code=502,
            error_code="cashflow_open_invoices_unavailable",
            user_message="Open invoice data could not be loaded from QuickBooks.",
            exc=exc,
            retryable=True,
            log_context="cashflow_open_invoices_unavailable",
        ) from exc
    try:
        open_bills = _fetch_open_bills(client)
    except Exception as exc:
        raise user_safe_http_exception(
            status_code=502,
            error_code="cashflow_open_bills_unavailable",
            user_message="Open bill data could not be loaded from QuickBooks.",
            exc=exc,
            retryable=True,
            log_context="cashflow_open_bills_unavailable",
        ) from exc
    partial_errors: list[dict] = []
    if haircut_error is not None:
        partial_errors.append(haircut_error)
    try:
        customer_dso = compute_customer_dso(client, months_back=12)
    except Exception as exc:
        partial_errors.append(
            safe_error_detail(
                error_code="cashflow_customer_dso_unavailable",
                user_message="Historical customer payment timing could not be loaded.",
                exc=exc,
                retryable=True,
                log_context="cashflow_customer_dso_unavailable",
            )
        )
        customer_dso = {}
    try:
        vendor_dpo = compute_vendor_dpo(client, months_back=12)
    except Exception as exc:
        partial_errors.append(
            safe_error_detail(
                error_code="cashflow_vendor_dpo_unavailable",
                user_message="Historical vendor payment timing could not be loaded.",
                exc=exc,
                retryable=True,
                log_context="cashflow_vendor_dpo_unavailable",
            )
        )
        vendor_dpo = {}

    # No hidden entity filtering here — that's applied live from overrides
    def _to_date(val) -> date:
        if isinstance(val, str):
            return date.fromisoformat(val[:10])
        elif hasattr(val, "date"):
            return val.date()
        return val

    # F-3: window-scoped override read (cells in the visible Fridays + all config sentinels).
    overrides = _load_window_overrides(db, company_id, fridays)
    sentinel_dates = {AVG_SENTINEL, DAY_SENTINEL, HIDDEN_SENTINEL, ORDER_SENTINEL, BALANCE_SENTINEL}
    override_map: Dict[tuple, float] = {
        (o.entity_name, o.entity_type, _to_date(o.week_ending)): float(o.override_amount)
        for o in overrides
    }

    # Build set of hidden entities
    hidden_entities = set()
    for o in overrides:
        if o.week_ending == HIDDEN_SENTINEL:
            hidden_entities.add((o.entity_name, o.entity_type))

    # Filter hidden from invoices/bills
    open_invoices = [i for i in open_invoices if (i["customer"], "customer") not in hidden_entities]
    open_bills = [b for b in open_bills if (b["vendor"], "vendor") not in hidden_entities]

    # Project invoices into weekly columns
    invoice_customer_rows = _project_invoices_into_weeks(
        open_invoices, fridays, customer_dso, override_map, "customer", forecast_start_idx=lookback
    )
    invoice_vendor_rows = _project_invoices_into_weeks(
        open_bills, fridays, vendor_dpo, override_map, "vendor", forecast_start_idx=lookback
    )

    invoice_customer_names = {r["name"] for r in invoice_customer_rows}
    invoice_vendor_names = {r["name"] for r in invoice_vendor_rows}

    # ── 2. Pattern-based projection ─────────────────────────────────────
    bank_balance, bank_balance_as_of, bank_balance_source, anchor_diagnostics = _resolve_bank_balance(
        client,
        company_id,
        db,
        fridays,
        lookback,
    )
    # PLAN-EXEMPT: 2026-07-09 honest-failure batch (AC-C4) — history-fetch failures land in
    # partial_errors so data_quality names them instead of an empty-history silent degrade.
    deposits = _fetch_deposits(client, since, errors=partial_errors)
    outflows = _fetch_outflows(client, since, errors=partial_errors)

    # One-off, sparse and unattributed history is not projected as a run-rate; it is
    # listed in the payload so the reviewer can model it explicitly.
    non_recurring_customers: list[dict] = []
    non_recurring_vendors: list[dict] = []
    customer_patterns = _aggregate_entity_patterns(
        deposits, as_of=date.today(), non_recurring=non_recurring_customers,
    )
    vendor_patterns = _aggregate_entity_patterns(
        outflows, as_of=date.today(), non_recurring=non_recurring_vendors,
    )
    non_recurring_history = [
        {**entry, "entity_type": "customer"} for entry in non_recurring_customers
    ] + [
        {**entry, "entity_type": "vendor"} for entry in non_recurring_vendors
    ]
    # Open items infer a run-rate only for payees the history has not already judged: an
    # entity paid in the window without a recurring cadence does not become monthly just
    # because it also has an open invoice or bill (its open items still project as known).
    non_recurring_customer_names = {entry["name"] for entry in non_recurring_customers}
    non_recurring_vendor_names = {entry["name"] for entry in non_recurring_vendors}
    inferred_customer_patterns = [
        p for p in _infer_run_rate_patterns_from_open_items(open_invoices, customer_dso, "customer")
        if p["name"] not in non_recurring_customer_names
    ]
    inferred_vendor_patterns = [
        p for p in _infer_run_rate_patterns_from_open_items(open_bills, vendor_dpo, "vendor")
        if p["name"] not in non_recurring_vendor_names
    ]
    customer_pattern_map = _combined_run_rate_pattern_map(customer_patterns, inferred_customer_patterns)
    vendor_pattern_map = _combined_run_rate_pattern_map(vendor_patterns, inferred_vendor_patterns)

    pure_customer_patterns = [
        p for p in customer_pattern_map.values()
        if p["name"] not in invoice_customer_names
        and (p["name"], "customer") not in hidden_entities
    ]
    pure_vendor_patterns = [
        p for p in vendor_pattern_map.values()
        if p["name"] not in invoice_vendor_names
        and (p["name"], "vendor") not in hidden_entities
    ]

    pattern_customer_rows = _project_into_weeks(pure_customer_patterns, fridays, override_map, "customer")
    pattern_vendor_rows = _project_into_weeks(pure_vendor_patterns, fridays, override_map, "vendor")

    # ── 2b. Hybrid fill ─────────────────────────────────────────────────
    pattern_override_map = {
        k: v for k, v in override_map.items()
        if k[2] in sentinel_dates
    }

    def _apply_run_rate_overlay(row, pattern_map, entity_type):
        pat = pattern_map.get(row["name"])
        if not pat:
            return
        pat_row = _project_into_weeks([pat], fridays, pattern_override_map, entity_type)
        if not pat_row:
            return

        projected = pat_row[0]
        known_amounts = list(row.get("known_amounts") or row.get("amounts") or [0.0] * len(fridays))
        if len(known_amounts) < len(fridays):
            known_amounts = known_amounts + [0.0] * (len(fridays) - len(known_amounts))
        known_amounts = known_amounts[:len(fridays)]
        known_covered_months = set(row.get("known_covered_months") or [])
        if not known_covered_months:
            known_covered_months = {
                _month_key(fridays[i])
                for i, amount in enumerate(known_amounts)
                if abs(float(amount or 0.0)) >= 0.01
            }

        run_rate_amounts = [0.0] * len(fridays)
        combined_amounts = [round(float(amount or 0.0), 2) for amount in known_amounts]
        for i, amount in enumerate(projected["amounts"]):
            if i < lookback:
                continue
            month_key = _month_key(fridays[i])
            if month_key in known_covered_months:
                continue
            recurring_amount = round(float(amount or 0.0), 2)
            if recurring_amount == 0.0:
                continue
            run_rate_amounts[i] = recurring_amount
            combined_amounts[i] = round(combined_amounts[i] + recurring_amount, 2)

        row["amounts"] = combined_amounts
        row["known_amounts"] = [round(float(amount or 0.0), 2) for amount in known_amounts]
        row["run_rate_amounts"] = run_rate_amounts
        row["known_covered_months"] = sorted(known_covered_months)
        row["run_rate_source"] = pat.get("run_rate_source", "history")
        # The cadence travels with the row so every later re-projection keeps it.
        row["interval_weeks"] = projected["interval_weeks"]
        row["anchor_date"] = projected["anchor_date"]
        row["day_of_month"] = projected["day_of_month"]
        row["effective_day"] = projected["effective_day"]
        row["avg_amount"] = round(float(pat["avg_amount"]), 2)
        row["effective_avg"] = projected["effective_avg"]
        row["source"] = "hybrid"

    for row in invoice_customer_rows:
        _apply_run_rate_overlay(row, customer_pattern_map, "customer")

    for row in invoice_vendor_rows:
        _apply_run_rate_overlay(row, vendor_pattern_map, "vendor")

    # Add source metadata to pattern rows
    for r in pattern_customer_rows:
        r["source"] = "pattern"
        r["open_invoices"] = 0
        r["total_open_balance"] = 0.0
        r["past_due_balance"] = 0.0
        r["dso_days"] = r.get("day_of_month", 0)
        r["known_amounts"] = [0.0] * len(fridays)
        r["run_rate_amounts"] = list(r.get("amounts", []))
        r["known_covered_months"] = []
    for r in pattern_vendor_rows:
        r["source"] = "pattern"
        r["open_invoices"] = 0
        r["total_open_balance"] = 0.0
        r["past_due_balance"] = 0.0
        r["dso_days"] = r.get("day_of_month", 0)
        r["known_amounts"] = [0.0] * len(fridays)
        r["run_rate_amounts"] = list(r.get("amounts", []))
        r["known_covered_months"] = []

    # ── 3. Merge ────────────────────────────────────────────────────────
    customer_rows = invoice_customer_rows + pattern_customer_rows
    vendor_rows = invoice_vendor_rows + pattern_vendor_rows

    # ── User-created entities (not in QBO) ──────────────────────────────
    skip_names = {"Other Cash In", "Non Routine Expenses"}
    all_customer_names = {r["name"] for r in customer_rows}
    all_vendor_names = {r["name"] for r in vendor_rows}

    user_entities: Dict[tuple, Dict] = {}
    for o in overrides:
        if o.entity_name in skip_names:
            continue
        if o.entity_type == "customer" and o.entity_name not in all_customer_names:
            key = (o.entity_name, "customer")
            if key in hidden_entities:
                continue
            if key not in user_entities:
                user_entities[key] = {"day": 1, "avg": 0.0}
            if o.week_ending == DAY_SENTINEL:
                user_entities[key]["day"] = int(o.override_amount)
            elif o.week_ending == AVG_SENTINEL:
                user_entities[key]["avg"] = float(o.override_amount)
        elif o.entity_type == "vendor" and o.entity_name not in all_vendor_names:
            key = (o.entity_name, "vendor")
            if key in hidden_entities:
                continue
            if key not in user_entities:
                user_entities[key] = {"day": 1, "avg": 0.0}
            if o.week_ending == DAY_SENTINEL:
                user_entities[key]["day"] = int(o.override_amount)
            elif o.week_ending == AVG_SENTINEL:
                user_entities[key]["avg"] = float(o.override_amount)

    for (name, etype), info in user_entities.items():
        pattern = [{"name": name, "day_of_month": info["day"], "avg_amount": info["avg"]}]
        rows = _project_into_weeks(pattern, fridays, override_map, etype)
        for r in rows:
            r["source"] = "manual"
            r["open_invoices"] = 0
            r["total_open_balance"] = 0.0
            r["past_due_balance"] = 0.0
            r["dso_days"] = 0
        if etype == "customer":
            customer_rows.extend(rows)
        else:
            vendor_rows.extend(rows)

    # Sort by persisted sort order
    customer_rows.sort(key=lambda r: r.get("sort_order", 999.0))
    vendor_rows.sort(key=lambda r: r.get("sort_order", 999.0))

    # PLAN-EXEMPT: 2026-07-09 honest-failure batch (AC-C3) — expected cash past the last
    # grid Friday is counted per row by _project_invoices_into_weeks; aggregate it here so
    # the payload (and its consumers) can say "this much open cash sits beyond the horizon"
    # instead of it silently vanishing from the grid totals.
    beyond_horizon = {
        "cash_in": round(sum(r.get("beyond_horizon_balance", 0.0) for r in customer_rows), 2),
        "cash_out": round(sum(r.get("beyond_horizon_balance", 0.0) for r in vendor_rows), 2),
        "item_count": sum(
            int(r.get("beyond_horizon_items", 0)) for r in customer_rows + vendor_rows
        ),
        "policy": "excluded_from_grid_and_reported",
    }
    if beyond_horizon["item_count"]:
        logger.info(
            "[CashForecast] realm=%s beyond-horizon open items excluded from the %s-week grid: "
            "count=%s cash_in=%.2f cash_out=%.2f",
            company_id,
            weeks,
            beyond_horizon["item_count"],
            beyond_horizon["cash_in"],
            beyond_horizon["cash_out"],
        )

    # Snapshot stores: entity rows + week headers (+ the per-tenant haircut provenance
    # so a consumer can see WHICH basis discounted the receivables — AC-3).
    snapshot_data = {
        "week_headers": [f.isoformat() for f in fridays],
        "lookback": lookback,
        "customer_rows": customer_rows,
        "vendor_rows": vendor_rows,
        "beyond_horizon": beyond_horizon,
        "non_recurring_history": non_recurring_history,
        "collection_haircuts": {
            "basis": collection_haircuts.get("basis"),
            "buckets": {
                str(floor): rate
                for floor, rate in (collection_haircuts.get("buckets") or {}).items()
            },
            "invoice_counts": {
                str(floor): count
                for floor, count in (collection_haircuts.get("invoice_counts") or {}).items()
            },
        },
        "errors": partial_errors,
        "data_quality": {
            "is_partial": bool(partial_errors),
            "unavailable_sections": [err["error_code"] for err in partial_errors],
        },
    }
    return snapshot_data, bank_balance, bank_balance_as_of, bank_balance_source, anchor_diagnostics


def _apply_live_layer(
    snapshot_data: Dict,
    bank_balance: float,
    company_id: str,
    weeks: int,
    lookback: int,
    db: Session,
    bank_balance_as_of: Optional[date] = None,
    bank_balance_source: str = "unknown",
    anchor_diagnostics: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Take raw snapshot data and apply user overrides, actuals reconciliation,
    hidden entity filtering, and rolling balance computation.
    This runs on EVERY load but is fast — no QBO calls.
    """
    import copy
    # Deep-copy so we don't mutate the cached snapshot
    customer_rows = copy.deepcopy(snapshot_data["customer_rows"])
    vendor_rows = copy.deepcopy(snapshot_data["vendor_rows"])

    # PLAN-EXEMPT: 2026-07-09 honest-failure batch (AC-C2/C4) — live-layer failure sink +
    # silent-drop counters; both are folded into the served payload so nothing that fails
    # or falls through this layer is invisible to the consumer.
    live_errors: "list[dict]" = []
    unknown_type_count = 0
    unknown_type_total = 0.0

    # IMPORTANT: Recompute fridays from the CURRENT date, not the snapshot's
    # stored week_headers.  The snapshot may have been built days/weeks ago,
    # so its Friday dates can be stale.  Actuals are stored with week_ending
    # relative to the current calendar, so we must match that.
    fridays = _week_fridays(weeks, lookback=lookback)
    total_cols = len(fridays)

    stored_fridays = [
        parsed for parsed in (_safe_date(raw) for raw in snapshot_data.get("week_headers", []))
        if parsed is not None
    ]

    def _align_amount_list(old_amounts: List[Any]) -> List[float]:
        if stored_fridays:
            by_week = {
                stored_fridays[i]: float(old_amounts[i] or 0.0)
                for i in range(min(len(stored_fridays), len(old_amounts)))
            }
            return [round(by_week.get(fri, 0.0), 2) for fri in fridays]
        if len(old_amounts) < total_cols:
            old_amounts = old_amounts + [0.0] * (total_cols - len(old_amounts))
        return [round(float(amount or 0.0), 2) for amount in old_amounts[:total_cols]]

    def _align_amounts_to_current_fridays(row: Dict[str, Any]) -> None:
        row["amounts"] = _align_amount_list(list(row.get("amounts") or []))
        for amount_key in ("known_amounts", "run_rate_amounts"):
            if amount_key in row:
                row[amount_key] = _align_amount_list(list(row.get(amount_key) or []))

    for row in customer_rows + vendor_rows:
        _align_amounts_to_current_fridays(row)

    # F-3: window-scoped override read — the visible Fridays' cell overrides + all config
    # sentinels + legacy balance rows (was an unfiltered full-per-company read on every load).
    overrides = _load_window_overrides(db, company_id, fridays)
    override_map: Dict[tuple, float] = {
        (o.entity_name, o.entity_type, _to_date(o.week_ending)): float(o.override_amount)
        for o in overrides
    }

    # Hidden entities
    hidden_entities = set()
    for o in overrides:
        if o.week_ending == HIDDEN_SENTINEL:
            hidden_entities.add((o.entity_name, o.entity_type))

    # Filter hidden
    customer_rows = [r for r in customer_rows if (r["name"], "customer") not in hidden_entities]
    vendor_rows = [r for r in vendor_rows if (r["name"], "vendor") not in hidden_entities]

    # ── Reconstruct user-created entities from overrides ───────────────
    # The snapshot may be stale (days old), so we must rebuild any rows
    # the user created via the UI that aren't already in the snapshot.
    # Mutual exclusion in save_override prevents NEW conflicts (HIDDEN + DAY).
    # The hidden check below handles LEGACY data where both sentinels coexist.
    # When re-adding, save_override deletes HIDDEN first, so this guard won't fire.
    skip_names = {"Other Cash In", "Non Routine Expenses", "__beginning_balance", "__excluded"}
    all_customer_names = {r["name"] for r in customer_rows}
    all_vendor_names = {r["name"] for r in vendor_rows}

    user_entities: Dict[tuple, Dict] = {}
    for o in overrides:
        if o.entity_name in skip_names:
            continue
        if o.week_ending == HIDDEN_SENTINEL or o.week_ending == ORDER_SENTINEL:
            continue  # these don't indicate entity creation
        if o.week_ending == BALANCE_SENTINEL:
            continue

        if o.entity_type == "customer" and o.entity_name not in all_customer_names:
            key = (o.entity_name, "customer")
            if key in hidden_entities:
                continue
            if key not in user_entities:
                user_entities[key] = {"day": 1, "avg": 0.0}
            if o.week_ending == DAY_SENTINEL:
                user_entities[key]["day"] = int(o.override_amount)
            elif o.week_ending == AVG_SENTINEL:
                user_entities[key]["avg"] = float(o.override_amount)
        elif o.entity_type == "vendor" and o.entity_name not in all_vendor_names:
            key = (o.entity_name, "vendor")
            if key in hidden_entities:
                continue
            if key not in user_entities:
                user_entities[key] = {"day": 1, "avg": 0.0}
            if o.week_ending == DAY_SENTINEL:
                user_entities[key]["day"] = int(o.override_amount)
            elif o.week_ending == AVG_SENTINEL:
                user_entities[key]["avg"] = float(o.override_amount)

    for (name, etype), info in user_entities.items():
        pattern = [{"name": name, "day_of_month": info["day"], "avg_amount": info["avg"]}]
        rows = _project_into_weeks(pattern, fridays, override_map, etype)
        for r in rows:
            r["source"] = "manual"
            r["open_invoices"] = 0
            r["total_open_balance"] = 0.0
            r["past_due_balance"] = 0.0
            r["dso_days"] = 0
        if etype == "customer":
            customer_rows.extend(rows)
            all_customer_names.add(name)
        else:
            vendor_rows.extend(rows)
            all_vendor_names.add(name)

    # ── Apply DAY/AVG overrides to existing snapshot rows ──────────────
    # The snapshot bakes in day_of_month and amounts at build time, so if
    # the user changed Day or Avg AFTER the snapshot, we must re-apply them
    # and re-project the amounts into the correct weekly columns.
    def _reproject_row(row: Dict, etype: str) -> None:
        day_key = (row["name"], etype, DAY_SENTINEL)
        avg_key = (row["name"], etype, AVG_SENTINEL)
        new_day = override_map.get(day_key)
        new_avg = override_map.get(avg_key)

        if row.get("source") == "hybrid" or ("known_amounts" in row and "run_rate_amounts" in row):
            entity_day = max(1, min(28, int(new_day))) if new_day is not None else row.get("effective_day", row.get("day_of_month", 15))
            effective_avg = new_avg if new_avg is not None else row.get("effective_avg", row.get("avg_amount", 0.0))
            sentinel_override_map = {
                key: value
                for key, value in override_map.items()
                if key[2] in {AVG_SENTINEL, DAY_SENTINEL}
            }
            projected = _project_into_weeks(
                [{
                    "name": row["name"],
                    "day_of_month": entity_day,
                    "interval_weeks": int(row.get("interval_weeks", MONTHLY)),
                    "anchor_date": row.get("anchor_date"),
                    "avg_amount": effective_avg,
                    "run_rate_source": row.get("run_rate_source", "history"),
                }],
                fridays,
                sentinel_override_map,
                etype,
            )
            if not projected:
                return

            known_amounts = list(row.get("known_amounts") or row.get("amounts") or [])
            if len(known_amounts) < total_cols:
                known_amounts = known_amounts + [0.0] * (total_cols - len(known_amounts))
            known_amounts = [round(float(amount or 0.0), 2) for amount in known_amounts[:total_cols]]
            known_covered_months = set(row.get("known_covered_months") or [])
            if not known_covered_months:
                known_covered_months = {
                    _month_key(fridays[i])
                    for i, amount in enumerate(known_amounts)
                    if abs(float(amount or 0.0)) >= 0.01
                }

            run_rate_amounts = [0.0] * total_cols
            for i in range(lookback, total_cols):
                cell_key = (row["name"], etype, fridays[i])
                if cell_key in override_map:
                    continue
                recurring_amount = round(float(projected[0]["amounts"][i] or 0.0), 2)
                if _month_key(fridays[i]) in known_covered_months:
                    recurring_amount = 0.0
                run_rate_amounts[i] = recurring_amount
                row["amounts"][i] = round(float(known_amounts[i] or 0.0) + recurring_amount, 2)

            row["known_amounts"] = known_amounts
            row["run_rate_amounts"] = run_rate_amounts
            row["known_covered_months"] = sorted(known_covered_months)
            row["effective_day"] = projected[0]["effective_day"]
            row["day_of_month"] = projected[0]["day_of_month"]
            row["effective_avg"] = projected[0]["effective_avg"]
            row["avg_amount"] = round(float(effective_avg or 0.0), 2)
            row["source"] = "hybrid"
            return

        if row.get("source") == "invoice":
            if new_day is not None:
                display_day = max(1, min(28, int(new_day)))
                row["effective_day"] = display_day
                row["day_of_month"] = display_day
            if new_avg is not None:
                row["effective_avg"] = round(new_avg, 2)
            return
        if row.get("source") == "actual":
            return

        entity_day = max(1, min(31, int(new_day))) if new_day is not None else row.get("effective_day", row.get("day_of_month", 15))
        effective_avg = new_avg if new_avg is not None else row.get("effective_avg", row.get("avg_amount", 0.0))
        interval_weeks = int(row.get("interval_weeks", MONTHLY))

        row["effective_day"] = entity_day
        row["day_of_month"] = entity_day
        row["effective_avg"] = round(effective_avg, 2)

        # Re-project the forecast columns on the entity's cadence (interval-week model); keep the
        # lookback actuals untouched. Cell-level overrides are applied later, so skip those weeks.
        projected = project_interval_amounts(
            fridays, interval_weeks=interval_weeks, entity_day=entity_day,
            monthly_avg=effective_avg, start_idx=lookback, end_idx=total_cols,
            anchor_date=_safe_date(row.get("anchor_date")),
        )
        for i in range(lookback, total_cols):
            cell_key = (row["name"], etype, fridays[i])
            if cell_key in override_map:
                continue
            row["amounts"][i] = projected.get(i, 0.0)  # SA-OK: projected is a plain dict, not a DB query (no I/O in this loop)

    for row in customer_rows:
        _reproject_row(row, "customer")
    for row in vendor_rows:
        _reproject_row(row, "vendor")

    # Apply cell-level overrides to forecast columns (non-lookback)
    for row in customer_rows:
        for i in range(lookback, total_cols):
            cell_key = (row["name"], "customer", fridays[i])
            if cell_key in override_map:
                row["amounts"][i] = override_map[cell_key]
        # Apply sort order
        order_key = (row["name"], "customer", ORDER_SENTINEL)
        if order_key in override_map:
            row["sort_order"] = override_map[order_key]

    for row in vendor_rows:
        for i in range(lookback, total_cols):
            cell_key = (row["name"], "vendor", fridays[i])
            if cell_key in override_map:
                row["amounts"][i] = override_map[cell_key]
        order_key = (row["name"], "vendor", ORDER_SENTINEL)
        if order_key in override_map:
            row["sort_order"] = override_map[order_key]

    # Re-sort after applying sort overrides
    customer_rows.sort(key=lambda r: r.get("sort_order", 999.0))
    vendor_rows.sort(key=lambda r: r.get("sort_order", 999.0))

    # Beginning balance
    beginning_balance_override = None
    if lookback == 0:
        for o in overrides:
            if o.entity_name == "__beginning_balance" and o.entity_type == BALANCE_OVERRIDE_TYPE:
                if o.week_ending == BALANCE_SENTINEL:
                    beginning_balance_override = float(o.override_amount)
                    break
                elif beginning_balance_override is None:
                    beginning_balance_override = float(o.override_amount)

    # Other Cash In / Non Routine Expenses manual rows
    other_in = [0.0] * total_cols
    other_out = [0.0] * total_cols
    for col in range(total_cols):
        key = ("Other Cash In", "customer", fridays[col])
        if key in override_map:
            other_in[col] = override_map[key]
        key = ("Non Routine Expenses", "vendor", fridays[col])
        if key in override_map:
            other_out[col] = override_map[key]

    # Compute totals (pre-actuals)
    total_in = [
        sum(row["amounts"][i] for row in customer_rows) + other_in[i]
        for i in range(total_cols)
    ]
    total_out = [
        sum(row["amounts"][i] for row in vendor_rows) + other_out[i]
        for i in range(total_cols)
    ]

    # Rolling bank balance
    beginning_balances, ending_balances = _compute_rolling_balances(
        total_in,
        total_out,
        beginning_balance_override if beginning_balance_override is not None else bank_balance,
        lookback,
    )
    reconciliation_client = None
    if lookback > 0 and bank_balance_as_of:
        try:
            reconciliation_client = get_qbo_client_from_db(db, company_id)
        except Exception as exc:
            # PLAN-EXEMPT: 2026-07-09 honest-failure batch (AC-C4) — redacted log + surfaced
            # in data_quality; the reconciliation is skipped but never silently absent.
            logger.warning(
                "[CashForecast] Reconciliation client fetch failed for %s: %s",
                company_id,
                redact_to_string(str(exc), max_length=300),
            )
            live_errors.append(safe_error_detail(
                error_code="cashflow_reconciliation_unavailable",
                user_message=(
                    "The lookback reconciliation could not be computed; the forecast is "
                    "served without the balance-sheet cross-check."
                ),
                exc=exc,
                retryable=True,
                log_context="cashflow_reconciliation_unavailable",
            ))
    reconciliation_delta = _compute_reconciliation_delta(
        reconciliation_client,
        company_id,
        db,
        bank_balance,
        bank_balance_as_of,
        total_in,
        total_out,
        lookback,
        fridays,
        bank_balance_source,
    )

    forecast_reanchored = False  # PLAN-EXEMPT: 2a CFF re-anchor flag (ratified 2026-06-25 "proceed with 1 and 2")

    # ── Reconcile actuals into lookback columns + cascade missed ────────
    week_statuses: List[Dict[str, Any]] = []
    if lookback > 0:
        lookback_fridays = fridays[:lookback]
        week_statuses = _week_status_summary(db, company_id, lookback_fridays)
        status_by_week = {
            date.fromisoformat(item["week_ending"]): item
            for item in week_statuses
        }
        actualized_week_idxs = {
            idx
            for idx, fri in enumerate(lookback_fridays)
            if status_by_week[fri]["status"] != WEEK_STATUS_NOT_PULLED
        }
        cascade_ready_week_idxs = {
            idx
            for idx, fri in enumerate(lookback_fridays)
            if status_by_week[fri]["cascade_ready"]
        }
        actuals = db.query(CashForecastActual).filter(
            CashForecastActual.company_id == company_id,
            CashForecastActual.week_ending.in_(lookback_fridays),
            CashForecastActual.category.in_(["categorized", "decomposed"]),
            CashForecastActual.parent_id == None,  # noqa: E711
        ).all()

        decomposed_children = db.query(CashForecastActual).filter(
            CashForecastActual.company_id == company_id,
            CashForecastActual.week_ending.in_(lookback_fridays),
            CashForecastActual.category == "categorized",
            CashForecastActual.parent_id != None,  # noqa: E711
        ).all()
        actuals_to_use = [a for a in actuals if a.category != "decomposed"] + list(decomposed_children)

        actuals_map: Dict[tuple, float] = defaultdict(float)
        for a in actuals_to_use:
            e_type = a.entity_type
            if not e_type and a.entity_name:
                e_type = "vendor" if a.direction == "out" else "customer"
            actuals_map[(a.entity_name, e_type, _to_date(a.week_ending))] += float(a.amount)

        # Phase 1: Zero lookback columns + track missed
        missed_customer: Dict[str, float] = defaultdict(float)
        missed_vendor: Dict[str, float] = defaultdict(float)

        for row in customer_rows:
            for col_idx in range(lookback):
                if col_idx not in actualized_week_idxs:
                    continue
                original = row["amounts"][col_idx]
                if col_idx in cascade_ready_week_idxs and original > 0:
                    missed_customer[row["name"]] += original
                row["amounts"][col_idx] = 0.0

        for row in vendor_rows:
            for col_idx in range(lookback):
                if col_idx not in actualized_week_idxs:
                    continue
                original = row["amounts"][col_idx]
                if col_idx in cascade_ready_week_idxs and original > 0:
                    missed_vendor[row["name"]] += original
                row["amounts"][col_idx] = 0.0

        for col_idx in range(lookback):
            if col_idx not in actualized_week_idxs:
                continue
            other_in[col_idx] = 0.0
            other_out[col_idx] = 0.0

        # Phase 2: Fill categorized actuals
        for row in customer_rows:
            for col_idx in range(lookback):
                if col_idx not in actualized_week_idxs:
                    continue
                fri = fridays[col_idx]
                key = (row["name"], "customer", fri)
                if key in actuals_map:
                    actual_amt = round(actuals_map[key], 2)
                    row["amounts"][col_idx] = actual_amt
                    if col_idx in cascade_ready_week_idxs:
                        missed_customer[row["name"]] -= actual_amt

        for row in vendor_rows:
            for col_idx in range(lookback):
                if col_idx not in actualized_week_idxs:
                    continue
                fri = fridays[col_idx]
                key = (row["name"], "vendor", fri)
                if key in actuals_map:
                    actual_amt = round(actuals_map[key], 2)
                    row["amounts"][col_idx] = actual_amt
                    if col_idx in cascade_ready_week_idxs:
                        missed_vendor[row["name"]] -= actual_amt

        for col_idx in range(lookback):
            if col_idx not in actualized_week_idxs:
                continue
            fri = fridays[col_idx]
            oci_key = ("Other Cash In", "other_cash_in", fri)
            if oci_key in actuals_map:
                other_in[col_idx] = round(actuals_map[oci_key], 2)
            oci_key2 = ("Other Cash In", "customer", fri)
            if oci_key2 in actuals_map:
                other_in[col_idx] += round(actuals_map[oci_key2], 2)
            nre_key = ("Non Routine Expenses", "non_routine", fri)
            if nre_key in actuals_map:
                other_out[col_idx] = round(actuals_map[nre_key], 2)
            nre_key2 = ("Non Routine Expenses", "vendor", fri)
            if nre_key2 in actuals_map:
                other_out[col_idx] += round(actuals_map[nre_key2], 2)

        # Phase 2b: Create rows for actuals with no matching forecast row
        existing_customer_names = {r["name"] for r in customer_rows}
        existing_vendor_names = {r["name"] for r in vendor_rows}
        skip_special = {"Other Cash In", "Non Routine Expenses", "__excluded"}

        for (entity_name, entity_type, week_end), actual_amt in actuals_map.items():
            if entity_name in skip_special or not entity_name:
                continue
            col_idx = None
            for ci in range(lookback):
                if fridays[ci] == week_end:
                    col_idx = ci
                    break
            if col_idx is None:
                continue
            if col_idx not in actualized_week_idxs:
                continue

            if entity_type in ("customer",) and entity_name not in existing_customer_names:
                # Don't re-create entities the user explicitly hid
                if (entity_name, "customer") in hidden_entities:
                    continue
                amounts = [0.0] * total_cols
                amounts[col_idx] = round(actual_amt, 2)
                customer_rows.append({
                    "name": entity_name,
                    "day_of_month": 0, "effective_day": 0,
                    "avg_amount": 0.0, "effective_avg": 0.0,
                    "amounts": amounts, "sort_order": 999.0,
                    "source": "actual", "open_invoices": 0,
                    "total_open_balance": 0.0, "dso_days": 0,
                })
                existing_customer_names.add(entity_name)

            elif entity_type in ("vendor",) and entity_name not in existing_vendor_names:
                # Don't re-create entities the user explicitly hid
                if (entity_name, "vendor") in hidden_entities:
                    continue
                amounts = [0.0] * total_cols
                amounts[col_idx] = round(actual_amt, 2)
                vendor_rows.append({
                    "name": entity_name,
                    "day_of_month": 0, "effective_day": 0,
                    "avg_amount": 0.0, "effective_avg": 0.0,
                    "amounts": amounts, "sort_order": 999.0,
                    "source": "actual", "open_invoices": 0,
                    "total_open_balance": 0.0, "dso_days": 0,
                })
                existing_vendor_names.add(entity_name)

            elif entity_type not in ("customer", "vendor"):
                # PLAN-EXEMPT: 2026-07-09 honest-failure batch (AC-C1/C2). An in-window
                # categorized actual whose entity_type matches no grid section used to fall
                # through BOTH branches silently: Phase 1 zeroed its week's column and no
                # fill branch could place it — the week's actuals were understated with no
                # record. Count + log + surface it (the user re-categorizes; we never guess).
                unknown_type_count += 1
                unknown_type_total = round(unknown_type_total + float(actual_amt), 2)
                logger.warning(
                    "[CashForecast] realm=%s categorized actual not reflected in grid: "
                    "entity=%s type=%s week=%s amount=%.2f reason=unknown_entity_type",
                    company_id,
                    entity_name,
                    entity_type,
                    week_end,
                    actual_amt,
                )

        # Phase 2c: REMOVED — lookback columns should always reflect actuals,
        # not stale cell overrides from when that Friday was a forecast column.


        # Apply Other Cash In / Non Routine Expenses overrides for FORECAST columns only
        # (lookback columns are driven by actuals, not overrides)
        for col_idx in range(lookback, total_cols):
            fri = fridays[col_idx]
            oci_key = ("Other Cash In", "customer", fri)
            if oci_key in override_map:
                other_in[col_idx] = override_map[oci_key]
            nre_key = ("Non Routine Expenses", "vendor", fri)
            if nre_key in override_map:
                other_out[col_idx] = override_map[nre_key]



        # Phase 3: Cascade missed into first forecast column
        first_forecast_col = lookback
        if first_forecast_col < total_cols:
            for row in customer_rows:
                missed_amt = missed_customer.get(row["name"], 0.0)
                if missed_amt > 0.01:
                    cell_key = (row["name"], "customer", fridays[first_forecast_col])
                    if cell_key not in override_map:
                        row["amounts"][first_forecast_col] = round(
                            row["amounts"][first_forecast_col] + missed_amt, 2
                        )

            for row in vendor_rows:
                missed_amt = missed_vendor.get(row["name"], 0.0)
                if missed_amt > 0.01:
                    cell_key = (row["name"], "vendor", fridays[first_forecast_col])
                    if cell_key not in override_map:
                        row["amounts"][first_forecast_col] = round(
                            row["amounts"][first_forecast_col] + missed_amt, 2
                        )

        # Recompute totals after reconciliation
        total_in = [
            sum(row["amounts"][i] for row in customer_rows) + other_in[i]
            for i in range(total_cols)
        ]
        total_out = [
            sum(row["amounts"][i] for row in vendor_rows) + other_out[i]
            for i in range(total_cols)
        ]
        beginning_balances, ending_balances = _compute_rolling_balances(
            total_in,
            total_out,
            beginning_balance_override if beginning_balance_override is not None else bank_balance,
            lookback,
        )
        reconciliation_delta = _compute_reconciliation_delta(
            reconciliation_client,
            company_id,
            db,
            bank_balance,
            bank_balance_as_of,
            total_in,
            total_out,
            lookback,
            fridays,
            bank_balance_source,
            # PLAN-EXEMPT: 2026-07-09 honest-failure batch (AC-C4) — this authoritative
            # post-actuals call reports its failure into the payload's data_quality.
            errors=live_errors,
        )
        # PLAN-EXEMPT: 2a CFF re-anchor (ratified 2026-06-25). The historical columns keep the
        # opening->actuals roll (surfacing the reconciliation delta); the FORWARD projection is
        # re-seeded from the control balance — actual reconciled cash at the last historical week —
        # so it no longer inherits the actuals-capture drift. Skipped when the user pinned an
        # explicit opening override (their choice wins).
        if (
            reconciliation_delta is not None
            and beginning_balance_override is None
            and lookback < total_cols
        ):
            beginning_balances, ending_balances = _reanchor_forecast_to_control(
                beginning_balances, ending_balances, total_in, total_out,
                lookback, reconciliation_delta["anchor_bank_balance"],
            )
            forecast_reanchored = True

    # Count uncategorized actuals for the banner
    uncategorized_count = 0
    if lookback > 0:
        uncategorized_count = db.query(CashForecastActual).filter(
            CashForecastActual.company_id == company_id,
            CashForecastActual.week_ending.in_(fridays[:lookback]),
            CashForecastActual.category == "uncategorized",
        ).count()

    past_due_in = round(sum(r.get("past_due_balance", 0) for r in customer_rows), 2)
    past_due_out = round(sum(r.get("past_due_balance", 0) for r in vendor_rows), 2)
    review_required_weeks = [
        item["week_ending"]
        for item in week_statuses
        if item["status"] in {WEEK_STATUS_NOT_PULLED, WEEK_STATUS_PENDING}
    ]
    errors = list(snapshot_data.get("errors") or []) + live_errors

    # PLAN-EXEMPT: 2026-07-09 honest-failure batch (AC-C1/C2) — the F-4 silent-drop set
    # (categorized entities with NO visible forecast row; their cash is absent from every
    # forward week) is folded into the served payload itself, so the grid, reports, and the
    # assistant all see "this forecast is missing N categorized items" instead of a quietly
    # short total. Detection reuses cff_integrity (no silent auto-create — the ratified W-5
    # decision stands: the user owns the modeling fix).
    unmapped_entities = list_unmapped_categorized_entities(db, company_id)
    if unmapped_entities:
        logger.warning(
            "[CashForecast] realm=%s %s categorized entities have no forecast row — their "
            "cash is NOT in this forecast: %s",
            company_id,
            len(unmapped_entities),
            "; ".join(
                f"{e['entity_name']} ({e['entity_type']}, reason=no_forecast_row, "
                f"amount={e['total_amount']:.2f})"
                for e in unmapped_entities[:5]
            ),
        )
    unmatched_reasons: "dict[str, dict]" = {}
    if unmapped_entities:
        unmatched_reasons["no_forecast_row"] = {
            "count": len(unmapped_entities),
            "total_amount": round(sum(e["total_amount"] for e in unmapped_entities), 2),
        }
    if unknown_type_count:
        unmatched_reasons["unknown_entity_type"] = {
            "count": unknown_type_count,
            "total_amount": unknown_type_total,
        }

    result = {
        "week_headers": [f.isoformat() for f in fridays],
        "lookback": lookback,
        "beginning_balances": [round(b, 2) for b in beginning_balances],
        "opening_balance_week_ending": fridays[0].isoformat() if fridays else None,
        "first_forecast_week_ending": fridays[lookback].isoformat() if lookback < len(fridays) else None,
        "customers": customer_rows,
        "other_cash_in": [round(a, 2) for a in other_in],
        "total_cash_in": [round(t, 2) for t in total_in],
        "vendors": vendor_rows,
        "other_cash_out": [round(a, 2) for a in other_out],
        "total_cash_out": [round(t, 2) for t in total_out],
        "ending_balances": ending_balances,
        "bank_balance": round(bank_balance, 2),
        "bank_balance_as_of": bank_balance_as_of.isoformat() if bank_balance_as_of else None,
        "bank_balance_source": bank_balance_source,
        "anchor_diagnostics": anchor_diagnostics,
        "reconciliation_delta": reconciliation_delta,
        # PLAN-EXEMPT: 2a — surfaces whether the forecast was re-anchored to reconciled cash (ratified 2026-06-25)
        "forecast_reanchored_to_control": forecast_reanchored,
        "beginning_balance_override": beginning_balance_override,
        "uncategorized_actuals": uncategorized_count,
        "week_statuses": week_statuses,
        "review_required_weeks": review_required_weeks,
        "past_due_in": past_due_in,
        "past_due_out": past_due_out,
        "errors": errors,
        "data_quality": {
            "is_partial": bool(errors),
            "unavailable_sections": [err["error_code"] for err in errors if isinstance(err, dict)],
        },
        # Per-tenant haircut provenance carried through from the snapshot build (AC-3).
        "collection_haircuts": snapshot_data.get("collection_haircuts"),
        # PLAN-EXEMPT: 2026-07-09 honest-failure batch (AC-C2/C3) — the honest-gap ledger:
        # every categorized item this forecast could NOT place, counted with reasons, plus
        # open cash expected beyond the grid horizon. None for beyond_horizon means the
        # serving snapshot predates the accounting (unknown), never a fabricated zero.
        "unmatched": {
            "count": len(unmapped_entities) + unknown_type_count,
            "reasons": unmatched_reasons,
            "entities": unmapped_entities,
        },
        "beyond_horizon": snapshot_data.get("beyond_horizon"),
        # History payees NOT projected (one-off, sparse, irregular or unattributed). None
        # means the serving snapshot predates this accounting (unknown), never "none".
        "non_recurring_history": snapshot_data.get("non_recurring_history"),
    }
    # Liquidity runway + minimum-cash week off the PRIMARY grid (RD-2 extend row):
    # every served payload carries it, so the snapshot/assistant consumers inherit it.
    result["liquidity"] = liquidity_from_payload(result)
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Entity List (for categorization autocomplete)
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/entities")
def get_entity_list(
    company_id: str = Query(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
) -> Dict[str, Any]:
    """
    Return all active QBO customers and vendors for the categorization
    dropdown. Results are cached per request — the frontend should call
    this once when the modal opens.
    """
    check_tenant_access("VIEWER", company_id=company_id, db=db, user=current_user)
    try:
        client = get_qbo_client_from_db(db, company_id)
    except Exception as e:
        raise user_safe_http_exception(
            status_code=502,
            error_code="qbo_connection_failed",
            user_message="Could not connect to QuickBooks for this company.",
            exc=e,
            retryable=True,
            log_context="cashflow_forecast_entities_qbo_connection_failed",
        )

    customers = []
    vendors = []

    # Fetch all active customers
    try:
        start = 1
        while True:
            resp = client.query(
                f"SELECT Id, DisplayName FROM Customer WHERE Active = true "
                f"STARTPOSITION {start} MAXRESULTS 500"
            )
            batch = resp.get("QueryResponse", {}).get("Customer", [])
            if not batch:
                break
            for c in batch:
                customers.append({
                    "id": c.get("Id", ""),
                    "name": c.get("DisplayName", ""),
                    "type": "customer",
                })
            if len(batch) < 500:
                break
            start += 500
    except Exception as e:
        logger.warning("[Entities] Customer fetch failed: %s", redact_to_string(str(e), max_length=500))

    # Fetch all active vendors
    try:
        start = 1
        while True:
            resp = client.query(
                f"SELECT Id, DisplayName FROM Vendor WHERE Active = true "
                f"STARTPOSITION {start} MAXRESULTS 500"
            )
            batch = resp.get("QueryResponse", {}).get("Vendor", [])
            if not batch:
                break
            for v in batch:
                vendors.append({
                    "id": v.get("Id", ""),
                    "name": v.get("DisplayName", ""),
                    "type": "vendor",
                })
            if len(batch) < 500:
                break
            start += 500
    except Exception as e:
        logger.warning("[Entities] Vendor fetch failed: %s", redact_to_string(str(e), max_length=500))

    return {
        "customers": sorted(customers, key=lambda x: x["name"].lower()),
        "vendors": sorted(vendors, key=lambda x: x["name"].lower()),
    }


# ─────────────────────────────────────────────────────────────────────────────
# CFF Integrity — categorized-but-unmapped entities (W-5, F-4)
# ─────────────────────────────────────────────────────────────────────────────
# PLAN-EXEMPT: ratified plan 20260702-qa-remediation W-5 (F-4 #5 — CFF integrity).
# Categorizing a transaction to an entity with no forecast row silently drops it from the
# forecast once the lookback window rolls past (the Phase-2b auto-create above is display-time
# only and lookback-window-bounded). These endpoints make that state VISIBLE and give the user
# a one-click remediation — no silent auto-create; the user owns the modeling decision.


@router.get("/integrity/unmapped")
def get_unmapped_entities(
    company_id: str = Query(...),
    since_weeks: int = Query(CFF_DEFAULT_SINCE_WEEKS, ge=1, le=52),
    db: Session = Depends(get_db),
    # AUTH-OK: authed — get_current_user_from_token (this file's standard auth dep) + the
    # check_tenant_access("VIEWER") tenant gate in the body; not a public route.
    current_user: User = Depends(get_current_user_from_token),
) -> dict[str, object]:
    """Entities with categorized bank actuals in the trailing window but no visible
    forecast row — the F-4 silent-drop set. Feeds the CFF page callout banner."""
    check_tenant_access("VIEWER", company_id=company_id, db=db, user=current_user)
    unmapped = list_unmapped_categorized_entities(db, company_id, since_weeks=since_weeks)
    return {
        "company_id": company_id,
        "since_weeks": since_weeks,
        "count": len(unmapped),
        "unmapped": list(unmapped),
    }


class AddForecastRowRequest(BaseModel):
    company_id: str
    entity_name: str
    entity_type: str  # "customer" | "vendor"


@router.post("/integrity/add-row")
def add_forecast_row(
    body: AddForecastRowRequest,
    db: Session = Depends(get_db),
    # AUTH-OK: authed — get_current_user_from_token (this file's standard auth dep); tenant
    # gate = save_override's check_tenant_access("EDITOR") on the same db/user; not public.
    current_user: User = Depends(get_current_user_from_token),
) -> dict[str, object]:
    """One-click remediation for an unmapped entity: create its forecast row via the
    EXISTING row-create path (the DAY sentinel override — the same call the grid's
    "+ Add" control makes; ``save_override`` enforces EDITOR tenant access and the
    HIDDEN/DAY mutual exclusion, so re-adding un-hides), then enqueue the background
    snapshot rebuild so the entity's open-invoice/pattern data gets baked in."""
    # PLAN-EXEMPT: CI-red hotfix (Steven-directed) — the in-handler tenant re-assert the
    # multi-tenancy convention requires on every mutating handler. save_override re-checks
    # the same gate downstream; this makes the guard explicit at the route boundary
    # (test_route_authorization_static) instead of transitive.
    check_tenant_access("EDITOR", company_id=body.company_id, db=db, user=current_user)
    entity_name = (body.entity_name or "").strip()
    entity_type = (body.entity_type or "").strip().lower()
    if not entity_name or entity_type not in {"customer", "vendor"}:
        raise HTTPException(
            status_code=400,
            detail=safe_error_detail(
                error_code="cff_add_row_invalid_entity",
                user_message="entity_name and entity_type ('customer' or 'vendor') are required.",
                log_context="cff_add_row_invalid_entity",
            ),
        )
    if entity_name in CFF_SKIP_ENTITY_NAMES:
        raise HTTPException(
            status_code=400,
            detail=safe_error_detail(
                error_code="cff_add_row_builtin_row",
                user_message=f"'{entity_name}' is a built-in forecast row and cannot be added as an entity.",
                log_context="cff_add_row_builtin_row",
            ),
        )

    save_result = save_override(
        OverrideRequest(
            company_id=body.company_id,
            entity_name=entity_name,
            entity_type=entity_type,
            week_ending=DAY_SENTINEL,
            override_amount=1,
            note="Added from unmapped-entity callout",
        ),
        db=db,
        current_user=current_user,
    )

    # Background snapshot refresh so the new row picks up invoice/pattern data. force=True
    # bypasses the 10-minute recently-built dedupe — the user just changed the row set.
    # Non-fatal on failure: the sentinel row alone already makes the entity render (the
    # user-entity reconstruction in _apply_live_layer) and its actuals map in by name.
    snapshot_refresh_enqueued = True
    try:
        from ..tasks.cash_forecast_refresh import refresh_realm_forecast_snapshot

        refresh_realm_forecast_snapshot.delay(
            qbo_realm_id=body.company_id, weeks=13, lookback=1, force=True,
        )
    except Exception as exc:  # noqa: BLE001 — a broker outage must not undo the row create
        snapshot_refresh_enqueued = False
        logger.error(
            "[CashForecast] add-row snapshot refresh enqueue failed for %s: %s",
            body.company_id, redact_to_string(str(exc), max_length=300),
        )

    return {
        "status": "row_created",
        "entity_name": entity_name,
        "entity_type": entity_type,
        "override_id": save_result["id"],
        "snapshot_refresh_enqueued": snapshot_refresh_enqueued,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Override Endpoints
# ─────────────────────────────────────────────────────────────────────────────

class OverrideRequest(BaseModel):
    company_id: str
    entity_name: str
    entity_type: str   # "customer" | "vendor"
    week_ending: date
    override_amount: float
    note: Optional[str] = None


class SpreadBalanceRequest(BaseModel):
    company_id: str
    entity_name: str
    entity_type: str   # "customer" | "vendor"
    total_amount: float
    periods: int
    cadence: str       # "weekly" | "monthly"
    start_week_ending: Optional[date] = None
    day_of_month: Optional[int] = None
    note: Optional[str] = None


def apply_spread_balance(
    db: Session,
    *,
    company_id: str,
    entity_name: str,
    entity_type: str,
    total_amount: float,
    periods: int,
    cadence: str,
    start_week_ending: Optional[date] = None,
    day_of_month: Optional[int] = None,
    note: Optional[str] = None,
) -> Dict[str, Any]:
    entity_name = (entity_name or "").strip()
    entity_type = (entity_type or "").strip()
    cadence = (cadence or "").strip().lower()
    note = note or "AI spread balance"
    if not entity_name or entity_type not in {"customer", "vendor"}:
        raise HTTPException(status_code=400, detail="entity_name and entity_type ('customer' or 'vendor') are required.")
    if cadence not in {"weekly", "monthly"}:
        raise HTTPException(status_code=400, detail="cadence must be 'weekly' or 'monthly'.")
    if periods <= 0:
        raise HTTPException(status_code=400, detail="periods must be greater than 0.")

    fridays = _week_fridays(13, 0)
    start_week = start_week_ending or fridays[0]
    if start_week not in fridays:
        raise HTTPException(
            status_code=400,
            detail=(
                f"start_week_ending {start_week.isoformat()} is not in the current 13-week forecast window. "
                f"Available Fridays: {', '.join(f.isoformat() for f in fridays)}"
            ),
        )

    start_idx = fridays.index(start_week)
    day_of_month = max(1, min(28, int(day_of_month or 15)))

    def _rounded_split(total: float, count: int) -> List[float]:
        base = round(total / count, 2)
        values = [base for _ in range(count)]
        remainder = round(total - sum(values), 2)
        if values:
            values[-1] = round(values[-1] + remainder, 2)
        return values

    weekly_amounts: List[Dict[str, Any]] = []
    if cadence == "weekly":
        available = len(fridays) - start_idx
        if periods > available:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"only {available} weekly buckets are available from {start_week.isoformat()} "
                    f"through {fridays[-1].isoformat()}."
                ),
            )
        for amount, week_ending in zip(_rounded_split(total_amount, periods), fridays[start_idx:start_idx + periods]):
            weekly_amounts.append({"week_ending": week_ending, "amount": amount})
        avg_amount = round((total_amount / periods) * 4.33, 2)
    else:
        start_monday = start_week - timedelta(days=4)
        amounts = _rounded_split(total_amount, periods)
        year = start_monday.year
        month = start_monday.month
        month_slots: List[tuple[date, float]] = []
        for idx in range(periods):
            target_day = min(day_of_month, calendar.monthrange(year, month)[1])
            target_date = date(year, month, target_day)
            week_idx = _week_index(target_date, fridays)
            if week_idx is None or week_idx < start_idx:
                week_idx = _week_index(start_monday, fridays)
            if week_idx is None:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"monthly spread extends beyond the current 13-week forecast window near "
                        f"{year:04d}-{month:02d}-{target_day:02d}."
                    ),
                )
            month_slots.append((fridays[week_idx], amounts[idx]))
            month += 1
            if month > 12:
                month = 1
                year += 1
        by_week: Dict[date, float] = {}
        for week_ending, amount in month_slots:
            by_week[week_ending] = round(by_week.get(week_ending, 0) + amount, 2)
        weekly_amounts = [
            {"week_ending": week_ending, "amount": amount}
            for week_ending, amount in sorted(by_week.items(), key=lambda item: item[0])
        ]
        avg_amount = round(total_amount / periods, 2)

    def _upsert(week_ending: date, amount: float) -> CashForecastOverride:
        existing = db.query(CashForecastOverride).filter(
            CashForecastOverride.company_id == company_id,
            CashForecastOverride.entity_name == entity_name,
            CashForecastOverride.entity_type == entity_type,
            CashForecastOverride.week_ending == week_ending,
        ).first()
        if existing:
            existing.override_amount = amount  # type: ignore[assignment]
            existing.note = note  # type: ignore[assignment]
            existing.created_by = "cashflow_spread"  # type: ignore[assignment]
            return existing
        row = CashForecastOverride(
            company_id=company_id,
            entity_name=entity_name,
            entity_type=entity_type,
            week_ending=week_ending,
            override_amount=amount,
            note=note,
            created_by="cashflow_spread",
        )
        db.add(row)
        return row

    _upsert(DAY_SENTINEL, float(day_of_month))
    _upsert(AVG_SENTINEL, avg_amount)
    for item in weekly_amounts:
        _upsert(item["week_ending"], float(item["amount"]))

    db.commit()
    return {
        "status": "saved",
        "entity_name": entity_name,
        "entity_type": entity_type,
        "total_amount": total_amount,
        "periods": periods,
        "cadence": cadence,
        "start_week_ending": start_week.isoformat(),
        "day_of_month": day_of_month,
        "avg_amount": avg_amount,
        "weekly_amounts": [
            {"week_ending": item["week_ending"].isoformat(), "amount": float(item["amount"])}
            for item in weekly_amounts
        ],
    }


@router.post("/override")
def save_override(
    body: OverrideRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
):
    check_tenant_access("EDITOR", company_id=body.company_id, db=db, user=current_user)
    # ── Mutual exclusion: HIDDEN vs DAY/AVG ───────────────────────────
    # Hiding an entity should delete its creation sentinels (DAY, AVG).
    # Re-adding (DAY/AVG) should delete the HIDDEN sentinel.
    if body.week_ending == HIDDEN_SENTINEL:
        # User is hiding this entity → delete DAY + AVG sentinels
        db.query(CashForecastOverride).filter(
            CashForecastOverride.company_id == body.company_id,
            CashForecastOverride.entity_name == body.entity_name,
            CashForecastOverride.entity_type == body.entity_type,
            CashForecastOverride.week_ending.in_([DAY_SENTINEL, AVG_SENTINEL]),
        ).delete(synchronize_session=False)
    elif body.week_ending in (DAY_SENTINEL, AVG_SENTINEL):
        # User is (re-)adding this entity → delete HIDDEN sentinel
        db.query(CashForecastOverride).filter(
            CashForecastOverride.company_id == body.company_id,
            CashForecastOverride.entity_name == body.entity_name,
            CashForecastOverride.entity_type == body.entity_type,
            CashForecastOverride.week_ending == HIDDEN_SENTINEL,
        ).delete(synchronize_session=False)

    existing = db.query(CashForecastOverride).filter(
        CashForecastOverride.company_id == body.company_id,
        CashForecastOverride.entity_name == body.entity_name,
        CashForecastOverride.entity_type == body.entity_type,
        CashForecastOverride.week_ending == body.week_ending,
    ).first()
    if existing:
        existing.override_amount = body.override_amount  # type: ignore[assignment]
        existing.note = body.note  # type: ignore[assignment]
    else:
        existing = CashForecastOverride(
            company_id=body.company_id,
            entity_name=body.entity_name,
            entity_type=body.entity_type,
            week_ending=body.week_ending,
            override_amount=body.override_amount,
            note=body.note,
        )
        db.add(existing)
    db.commit()
    db.refresh(existing)
    return {"id": existing.id, "status": "saved"}


@router.post("/spread-balance")
def spread_balance(
    body: SpreadBalanceRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
):
    check_tenant_access("EDITOR", company_id=body.company_id, db=db, user=current_user)
    return apply_spread_balance(
        db,
        company_id=body.company_id,
        entity_name=body.entity_name,
        entity_type=body.entity_type,
        total_amount=body.total_amount,
        periods=body.periods,
        cadence=body.cadence,
        start_week_ending=body.start_week_ending,
        day_of_month=body.day_of_month,
        note=body.note,
    )


@router.delete("/override/{override_id}")
def delete_override(
    override_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
):
    row = check_entity_tenant_access(
        "EDITOR",
        db=db,
        user=current_user,
        model=CashForecastOverride,
        entity_id=override_id,
        not_found_detail="Override not found",
    )
    db.delete(row)
    db.commit()
    return {"status": "deleted"}


@router.delete("/entity-overrides")
def delete_entity_overrides(
    company_id: str = Query(...),
    entity_name: str = Query(...),
    entity_type: str = Query(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
):
    """Delete ALL overrides for a given entity (used when removing an entity row)."""
    check_tenant_access("EDITOR", company_id=company_id, db=db, user=current_user)
    rows = db.query(CashForecastOverride).filter(
        CashForecastOverride.company_id == company_id,
        CashForecastOverride.entity_name == entity_name,
        CashForecastOverride.entity_type == entity_type,
    ).all()
    count = len(rows)
    for r in rows:
        db.delete(r)
    db.commit()
    return {"status": "deleted", "count": count}


# ─────────────────────────────────────────────────────────────────────────────
# Actuals Endpoints — Weekly Bank Transaction Categorization
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/actuals/pull")
def pull_weekly_actuals(
    company_id: str = Query(...),
    week_ending: Optional[date] = Query(None, description="Friday of the target week. Defaults to last week."),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
) -> Dict[str, Any]:
    """
    Pull bank transactions for a given week from QBO and save them
    as CashForecastActual records for user categorization.

    Only pulls transactions that hit bank accounts (excludes CC charges).
    Auto-categorizes based on QBO entity refs where possible. The pull itself
    lives in ``app.cashflow.actuals_pull`` (shared with the transaction_review
    agent objective); this route authorizes and maps errors to HTTP.
    """
    check_tenant_access("EDITOR", company_id=company_id, db=db, user=current_user)
    try:
        client = get_qbo_client_from_db(db, company_id)
    except Exception as e:
        raise user_safe_http_exception(
            status_code=502,
            error_code="qbo_connection_failed",
            user_message="Could not connect to QuickBooks for this company.",
            exc=e,
            retryable=True,
            log_context="cashflow_forecast_categorize_qbo_connection_failed",
        )
    try:
        return pull_week_actuals(db, company_id, week_ending, client=client)
    except NoCashAccountsError as exc:
        raise HTTPException(status_code=400, detail=exc.detail) from exc


# PLAN-EXEMPT: ratified plan 20260702-qa-remediation W-5 AC-3 (F-4 — warn at source).
def _categorized_entity_is_unmapped(
    db: Session, record: CashForecastActual, index: ForecastRowIndex | None = None,
) -> bool:
    """True when this categorized actual's entity will NOT render in the forecast grid —
    no snapshot row, no user-created (DAY/AVG sentinel) row, or explicitly hidden (the F-4
    silent drop). Only meaningful for a real customer/vendor categorization; ``index`` lets
    the bulk path reuse one ``ForecastRowIndex`` per company instead of reloading per row."""
    if record.category != "categorized":
        return False
    entity_name = record.entity_name
    entity_type = record.entity_type
    if not entity_name or entity_type not in ("customer", "vendor"):
        return False
    if entity_name in CFF_SKIP_ENTITY_NAMES:
        return False
    if index is None:
        index = load_forecast_row_index(db, record.company_id)
    return not index.is_visible(entity_name, entity_type)


class CategorizeRequest(BaseModel):
    id: int
    entity_name: str
    entity_type: str  # "customer" | "vendor"
    category: str = "categorized"  # categorized | excluded


@router.post("/actuals/categorize")
def categorize_actual(
    body: CategorizeRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
):
    """Save user categorization for a bank transaction."""
    record = check_entity_tenant_access(
        "EDITOR",
        db=db,
        user=current_user,
        model=CashForecastActual,
        entity_id=body.id,
        not_found_detail="Actual not found",
    )

    # PLAN-EXEMPT: B5-6 P1 field bug (live-prod-corroborated 2026-06-24).
    # Normalize entity_type/category to canonical casing so a first-time-categorized
    # customer/vendor matches the Phase-2b forecast-row auto-create gate (which keys on
    # exact "customer"/"vendor" + category in {categorized,decomposed}); a raw "Customer"
    # would otherwise persist but never get a forecast row → an orphan reconciliation delta.
    from ..assistant.handlers import _normalize_cashflow_actual_entity, _normalize_cashflow_actual_category
    norm_name, norm_type = _normalize_cashflow_actual_entity(body.entity_name, body.entity_type)
    record.entity_name = norm_name if norm_name is not None else body.entity_name  # type: ignore[assignment]
    record.entity_type = norm_type if norm_type is not None else body.entity_type  # type: ignore[assignment]
    record.category = _normalize_cashflow_actual_category(body.category) or body.category  # type: ignore[assignment]
    db.commit()
    _sync_week_status(db, record.company_id, _to_date(record.week_ending), commit=True)
    db.refresh(record)
    # PLAN-EXEMPT: ratified plan 20260702-qa-remediation W-5 AC-3 (F-4 — warn at source).
    # Additive flag: True when the just-categorized entity has no visible forecast row, so
    # this money will NOT appear in the cash forecast (the F-4 silent drop). Non-blocking —
    # the frontend surfaces a warning with a one-click "add row"; existing callers that
    # ignore the key are unaffected.
    return {
        "id": record.id,
        "status": "categorized",
        "cff_unmapped": _categorized_entity_is_unmapped(db, record),
    }


class BulkCategorizeRequest(BaseModel):
    items: List[CategorizeRequest]


@router.post("/actuals/categorize-bulk")
def categorize_actuals_bulk(
    body: BulkCategorizeRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
):
    """Bulk categorize multiple bank transactions at once."""
    # PLAN-EXEMPT: B5-6 P1 field bug (live-prod-corroborated 2026-06-24) — normalize so
    # bulk-categorized customers/vendors hit the Phase-2b forecast-row auto-create gate.
    from ..assistant.handlers import _normalize_cashflow_actual_entity, _normalize_cashflow_actual_category
    results = []
    touched_records: List[CashForecastActual] = []
    for item in body.items:
        try:
            record = check_entity_tenant_access(
                "EDITOR",
                db=db,
                user=current_user,
                model=CashForecastActual,
                entity_id=item.id,
                not_found_detail="Actual not found",
            )
            # PLAN-EXEMPT: B5-6 P1 field bug (live-prod-corroborated 2026-06-24).
            norm_name, norm_type = _normalize_cashflow_actual_entity(item.entity_name, item.entity_type)
            record.entity_name = norm_name if norm_name is not None else item.entity_name  # type: ignore[assignment]
            record.entity_type = norm_type if norm_type is not None else item.entity_type  # type: ignore[assignment]
            record.category = _normalize_cashflow_actual_category(item.category) or item.category  # type: ignore[assignment]
            touched_records.append(record)
            results.append({"id": record.id, "status": "categorized"})
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
            results.append({"id": item.id, "status": "not_found"})
    db.commit()
    touched_weeks = {
        (record.company_id, _to_date(record.week_ending))
        for record in touched_records
    }
    for company_id, week_ending in touched_weeks:
        _sync_week_status(db, company_id, week_ending)
    db.commit()
    # PLAN-EXEMPT: ratified plan 20260702-qa-remediation W-5 AC-3 (F-4 — warn at source).
    # Additive per-item flag, mirroring the single-categorize endpoint: True when the
    # categorized entity has no visible forecast row. One ForecastRowIndex per company.
    record_by_id = {record.id: record for record in touched_records}
    row_index_by_company: dict[str, ForecastRowIndex] = {}
    for item in results:
        if item.get("status") != "categorized":
            continue
        record = record_by_id.get(item["id"])
        if record is None:
            continue
        index = row_index_by_company.get(record.company_id)
        if index is None:
            # SA-OK: bounded-loop 1 — memoized per distinct company_id in the batch (bulk
            # categorize is a single-company modal flow), not a per-row query.
            index = load_forecast_row_index(db, record.company_id)
            row_index_by_company[record.company_id] = index
        item["cff_unmapped"] = _categorized_entity_is_unmapped(db, record, index=index)
    return {"results": results}


class DecomposeRequest(BaseModel):
    parent_id: int
    vendor_breakdown: List[Dict[str, Any]]  # [{vendor_name, amount}]


@router.post("/actuals/decompose-billcom")
def decompose_billcom(
    body: DecomposeRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
):
    """
    Decompose a Bill.com lump-sum payment into per-vendor sub-rows.
    The parent record gets category='decomposed' and child records
    are created for each vendor.
    """
    parent = check_entity_tenant_access(
        "EDITOR",
        db=db,
        user=current_user,
        model=CashForecastActual,
        entity_id=body.parent_id,
        not_found_detail="Parent transaction not found",
    )
    if not parent.is_billcom:
        raise HTTPException(status_code=400, detail="Transaction is not a Bill.com payment")

    # Mark parent as decomposed
    parent.category = "decomposed"  # type: ignore[assignment]

    # Create child records
    children = []
    for vendor in body.vendor_breakdown:
        child = CashForecastActual(
            company_id=parent.company_id,
            qbo_txn_id=parent.qbo_txn_id,
            qbo_txn_type="BillPayment-BillCom",
            txn_date=parent.txn_date,
            amount=abs(float(vendor["amount"])),
            direction="out",
            description=vendor.get("vendor_name", ""),
            qbo_account_id=parent.qbo_account_id,
            account_name=parent.account_name,
            entity_name=vendor.get("vendor_name"),
            entity_type="vendor",
            category="categorized",
            is_billcom=True,
            parent_id=parent.id,
            week_ending=parent.week_ending,
        )
        db.add(child)
        children.append(child)

    db.commit()
    for c in children:
        db.refresh(c)
    _sync_week_status(db, parent.company_id, _to_date(parent.week_ending), commit=True)

    return {
        "parent_id": parent.id,
        "children_created": len(children),
        "children": [
            {
                "id": c.id,
                "vendor_name": c.entity_name,
                "amount": float(c.amount),
            }
            for c in children
        ],
    }


@router.post("/actuals/close")
def close_week_actuals(
    company_id: str = Query(...),
    week_ending: date = Query(...),
    force: bool = Query(False, description="Close anyway despite a partial QBO pull for this week."),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
) -> Dict[str, Any]:
    """Mark a pulled weekly actuals review as closed once categorization is complete."""
    check_tenant_access("MANAGER", company_id=company_id, db=db, user=current_user)
    counts = _actuals_counts_for_week(db, company_id, week_ending)
    status_row = db.query(CashForecastWeekStatus).filter(
        CashForecastWeekStatus.company_id == company_id,
        CashForecastWeekStatus.week_ending == week_ending,
    ).first()
    if not status_row and counts["transaction_count"] == 0:
        raise HTTPException(status_code=400, detail="Pull actuals for this week before closing review.")
    if counts["uncategorized_count"] > 0:
        raise HTTPException(
            status_code=400,
            detail=f"{counts['uncategorized_count']} transaction(s) still need categorization.",
        )
    # 2A: do not let a week be closed on a silently-incomplete pull. The pull
    # persists pull_partial when a QBO source failed; require an explicit force.
    if status_row is not None and status_row.pull_partial and not force:
        # LAYER-OK: bare 409 matches the legacy bare-400 raises above in this handler
        raise HTTPException(
            status_code=409,
            detail=(
                "This week's actuals pull was incomplete — one or more QuickBooks sources "
                "failed to load, so some transactions may be missing. Re-pull the week, or "
                "pass force=true to close anyway."
            ),
        )
    row = _sync_week_status(
        db,
        company_id,
        week_ending,
        explicit_status=WEEK_STATUS_CLOSED,
        note="Weekly actuals review closed",
        commit=True,
    )
    return {
        "week_ending": week_ending.isoformat(),
        "status": row.status,
        "transaction_count": counts["transaction_count"],
        "uncategorized_count": counts["uncategorized_count"],
    }


@router.get("/actuals/{week_ending_str}")
def get_week_actuals(
    week_ending_str: str,
    company_id: str = Query(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
) -> Dict[str, Any]:
    """Get all categorized actuals for a specific week."""
    check_tenant_access("VIEWER", company_id=company_id, db=db, user=current_user)
    try:
        target_week = date.fromisoformat(week_ending_str)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD.")

    records = db.query(CashForecastActual).filter(
        CashForecastActual.company_id == company_id,
        CashForecastActual.week_ending == target_week,
    ).all()

    return {
        "week_ending": target_week.isoformat(),
        "count": len(records),
        "transactions": [_actual_record_payload(r) for r in records],
        "summary": {
            "total_in": sum(float(r.amount) for r in records if r.direction == "in" and r.category not in ("excluded", "decomposed")),
            "total_out": sum(float(r.amount) for r in records if r.direction == "out" and r.category not in ("excluded", "decomposed")),
            "categorized": sum(1 for r in records if r.category == "categorized"),
            "uncategorized": sum(1 for r in records if r.category == "uncategorized"),
            "excluded": sum(1 for r in records if r.category == "excluded"),
        },
    }


@router.delete("/actuals/reset")
def reset_week_actuals(
    company_id: str = Query(...),
    week_ending: date = Query(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
):
    """Delete all actuals for a week to allow re-pulling from QBO."""
    check_tenant_access("EDITOR", company_id=company_id, db=db, user=current_user)
    rows = db.query(CashForecastActual).filter(
        CashForecastActual.company_id == company_id,
        CashForecastActual.week_ending == week_ending,
    ).all()
    count = len(rows)
    for r in rows:
        db.delete(r)
    status_row = db.query(CashForecastWeekStatus).filter(
        CashForecastWeekStatus.company_id == company_id,
        CashForecastWeekStatus.week_ending == week_ending,
    ).first()
    if status_row:
        status_row.status = WEEK_STATUS_NOT_PULLED  # type: ignore[assignment]
        status_row.transaction_count = 0  # type: ignore[assignment]
        status_row.uncategorized_count = 0  # type: ignore[assignment]
        status_row.note = "Actuals reset"  # type: ignore[assignment]
        status_row.pulled_at = None  # type: ignore[assignment]
        status_row.reviewed_at = None  # type: ignore[assignment]
        status_row.reconciled_at = None  # type: ignore[assignment]
    db.commit()
    return {"status": "reset", "deleted": count}

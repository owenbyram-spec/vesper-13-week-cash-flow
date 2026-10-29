"""
app/analysis/predictive_cash.py
───────────────────────────────
13-week predictive cash flow forecast engine.

Uses historical payment/collection patterns, AR/AP aging, and seasonality
to project weekly cash positions. Statistical approach (weighted averages),
not ML, which is appropriate for typical SMB QBO data volumes.
"""
from __future__ import annotations

import logging
import statistics
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from ..services.errors import safe_error_detail
from ..services.redaction import redact_to_string

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Forecast parameters (AP-FIN-09: named, labeled, output-visible — never buried
# inline in the math). Every constant below is surfaced verbatim in the returned
# `assumptions.parameters` block with a `basis` label (mirroring the
# collection-haircut `basis` idiom in app/analysis/payment_behavior.py /
# app/routers/cashflow_forecast.py), so no consumer — human or agent — receives
# a haircutted or derived number without seeing the knob that produced it.
# These are heuristic defaults, NOT company-calibrated: the per-tenant
# calibrated path is derive_collection_haircuts (basis "payment_history").
# Changing any VALUE here is a re-tune, not a transparency fix — it must be a
# deliberate change that re-pins the golden tests
# (tests/test_predictive_cash_golden.py).
# ─────────────────────────────────────────────────────────────────────────────

FORECAST_WEEKS = 13
"""Forecast horizon in weeks — the standard treasury 13-week cash-forecast
window this engine is named for. Convention, not a tuning knob."""

WEEKS_PER_MONTH = 4.33
"""Monthly→weekly conversion divisor. Deliberately the legacy 4.33
approximation, NOT the exact 52/12 (≈ 4.3333) convention used by
app/cashflow/projection.py: switching to 52/12 would shift every weekly flow
by ~0.077% and this constant was extracted in a transparency-only wave whose
contract is byte-identical output numbers. Adopting 52/12 is a separate,
deliberate re-tune that re-pins the golden tests."""

AR_COLLECTION_HAIRCUT = 0.85
"""Assumed share of current-bucket AR collected within the near-term window;
heuristic default, NOT company-calibrated (contrast:
derive_collection_haircuts, which derives per-tenant rates from
Payment↔Invoice history)."""

AP_PAYMENT_HAIRCUT = 0.90
"""Assumed share of current-bucket AP actually paid within the near-term
window; heuristic default, NOT company-calibrated."""

NEAR_TERM_WEEKS = 4
"""AR collections / AP payments estimated from aging are spread evenly across
this many leading forecast weeks (the near-term visibility window); heuristic
default."""

CONFIDENCE_BASE = 0.95
"""Week-1 forecast confidence; heuristic default, NOT a back-tested
calibration."""

CONFIDENCE_DECAY_PER_WEEK = 0.035
"""Linear per-week confidence decay; heuristic default. Week 13 ⇒
0.95 − 12 × 0.035 = 0.53."""

CONFIDENCE_FLOOR = 0.50
"""Lower bound on reported confidence; heuristic default."""

SEASONALITY_MIN_MONTHS = 6
"""Minimum months of P&L history before per-month seasonality factors are
computed; below this the forecast runs unseasonalized (neutral 1.0 factor).
Heuristic default."""

AGING_PERIOD_DAYS = "30"
"""QBO AgedReceivables/AgedPayables `aging_period` request parameter (days per
aging bucket); the "current" bucket read below is 0–30 days. QBO report
parameter, not a tuning knob."""


def _forecast_parameters() -> List[Dict[str, Any]]:
    """Every fixed parameter this engine applies, as output-visible records:
    name + value + basis label + what it does. Returned verbatim inside the
    payload's `assumptions.parameters` so haircuts are never invisible."""
    return [
        {
            "name": "ar_collection_haircut",
            "value": AR_COLLECTION_HAIRCUT,
            "basis": "heuristic_default",
            "description": (
                "Share of current-bucket AR assumed collected in the near-term "
                "window; not company-calibrated."
            ),
        },
        {
            "name": "ap_payment_haircut",
            "value": AP_PAYMENT_HAIRCUT,
            "basis": "heuristic_default",
            "description": (
                "Share of current-bucket AP assumed paid in the near-term "
                "window; not company-calibrated."
            ),
        },
        {
            "name": "weeks_per_month",
            "value": WEEKS_PER_MONTH,
            "basis": "calendar_approximation",
            "description": (
                "Monthly-to-weekly conversion divisor (legacy 4.33, "
                "intentionally not 52/12)."
            ),
        },
        {
            "name": "near_term_weeks",
            "value": NEAR_TERM_WEEKS,
            "basis": "heuristic_default",
            "description": (
                "AR/AP aging flows are spread evenly across the first N "
                "forecast weeks."
            ),
        },
        {
            "name": "confidence_base",
            "value": CONFIDENCE_BASE,
            "basis": "heuristic_default",
            "description": "Week-1 confidence before linear decay.",
        },
        {
            "name": "confidence_decay_per_week",
            "value": CONFIDENCE_DECAY_PER_WEEK,
            "basis": "heuristic_default",
            "description": "Linear per-week confidence decay.",
        },
        {
            "name": "confidence_floor",
            "value": CONFIDENCE_FLOOR,
            "basis": "heuristic_default",
            "description": "Lower bound on reported confidence.",
        },
        {
            "name": "seasonality_min_months",
            "value": SEASONALITY_MIN_MONTHS,
            "basis": "heuristic_default",
            "description": (
                "Minimum months of history before seasonality factors are "
                "applied; below this the forecast is unseasonalized."
            ),
        },
        {
            "name": "forecast_weeks",
            "value": FORECAST_WEEKS,
            "basis": "convention",
            "description": "Standard 13-week treasury cash-forecast horizon.",
        },
        {
            "name": "aging_period_days",
            "value": AGING_PERIOD_DAYS,
            "basis": "qbo_report_parameter",
            "description": (
                "QBO aged-report bucket width; the near-term AR/AP estimate "
                "reads the 0-30 day (current) bucket."
            ),
        },
    ]


def predict_13_week_cash(
    qbo_client,
    db=None,
    realm_id: str = "",
    starting_cash: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Generate a 13-week predictive cash flow forecast.

    Steps:
    1. Pull current cash balance from Balance Sheet
    2. Analyze last 12 months of cash-in (revenue) and cash-out (expenses)
    3. Compute weekly averages with recency weighting
    4. Apply seasonality factors (same-period-last-year comparison)
    5. Factor in AR aging (expected collections) and AP aging (expected payments)
    6. Output weekly projections: beginning balance, cash in, cash out, ending balance

    Returns:
        {
            "starting_cash": float,
            "forecast": [
                {"week": 1, "week_start": "2026-04-01", "week_end": "2026-04-07",
                 "beginning_balance": X, "cash_in": Y, "cash_out": Z,
                 "net_change": Y-Z, "ending_balance": X+Y-Z,
                 "confidence": 0.85}
            ],
            "summary": {
                "lowest_balance": float,
                "lowest_balance_week": int,
                "negative_cash_weeks": [int],
                "total_cash_in": float,
                "total_cash_out": float,
                "average_weekly_burn": float,
                "runway_weeks": int or None,
            },
            "assumptions": {
                ...,
                "parameters": [  # every fixed haircut/factor applied
                    {"name": str, "value": ..., "basis": str, "description": str},
                ],
            },
        }
    """
    today = date.today()

    # ── 1. Get current cash balance ──────────────────────────────────────
    current_cash = starting_cash
    if current_cash is None:
        current_cash = _get_current_cash(qbo_client)

    # ── 2. Pull historical monthly data ─────────────────────────────────
    monthly_data = _get_monthly_cash_flows(qbo_client)

    # ── 3. Compute weekly patterns ──────────────────────────────────────
    weekly_cash_in, weekly_cash_out = _compute_weekly_patterns(monthly_data)

    # ── 4. Apply seasonality ────────────────────────────────────────────
    seasonality = _compute_seasonality(monthly_data, today)

    # ── 5. Factor AR/AP aging ───────────────────────────────────────────
    ar_collections, ap_payments, near_term_errors = _estimate_ar_ap_cash_flows(qbo_client)

    # ── 6. Build 13-week forecast ───────────────────────────────────────
    forecast = []
    running_balance = current_cash

    # Calculate the start of the next Monday
    days_until_monday = (7 - today.weekday()) % 7
    if days_until_monday == 0:
        days_until_monday = 7
    forecast_start = today + timedelta(days=days_until_monday)

    for week_num in range(1, FORECAST_WEEKS + 1):
        week_start = forecast_start + timedelta(weeks=week_num - 1)
        week_end = week_start + timedelta(days=6)

        # Base weekly flows with recency weighting
        base_in = weekly_cash_in
        base_out = weekly_cash_out

        # Apply seasonality factor for this month (1.0 = neutral/no adjustment,
        # the identity — used when the month has no computed factor)
        month_key = week_start.month
        season_factor = seasonality.get(month_key, 1.0)
        adjusted_in = base_in * season_factor
        adjusted_out = base_out * season_factor

        # Add AR/AP known flows for the near-term window (near-term visibility)
        ar_boost = 0.0
        ap_drag = 0.0
        if week_num <= NEAR_TERM_WEEKS:
            # Spread known AR collections evenly across the near-term weeks
            ar_boost = ar_collections / NEAR_TERM_WEEKS
            ap_drag = ap_payments / NEAR_TERM_WEEKS

        total_in = adjusted_in + ar_boost
        total_out = adjusted_out + ap_drag
        net_change = total_in - total_out

        # Confidence decays linearly with time horizon, floored
        confidence = max(
            CONFIDENCE_FLOOR,
            CONFIDENCE_BASE - (week_num - 1) * CONFIDENCE_DECAY_PER_WEEK,
        )

        beginning = running_balance
        ending = beginning + net_change
        running_balance = ending

        forecast.append({
            "week": week_num,
            "week_start": week_start.isoformat(),
            "week_end": week_end.isoformat(),
            "beginning_balance": round(beginning, 2),
            "cash_in": round(total_in, 2),
            "cash_out": round(total_out, 2),
            "net_change": round(net_change, 2),
            "ending_balance": round(ending, 2),
            "confidence": round(confidence, 2),
        })

    # ── 7. Compute summary statistics ───────────────────────────────────
    balances = [w["ending_balance"] for w in forecast]
    lowest_balance = min(balances)
    lowest_week = balances.index(lowest_balance) + 1
    negative_weeks = [w["week"] for w in forecast if w["ending_balance"] < 0]

    total_in = sum(w["cash_in"] for w in forecast)
    total_out = sum(w["cash_out"] for w in forecast)
    avg_weekly_burn = (total_out - total_in) / FORECAST_WEEKS if total_out > total_in else 0

    # Runway: weeks until cash hits 0 at current burn rate
    runway_weeks = None
    if avg_weekly_burn > 0 and current_cash > 0:
        runway_weeks = int(current_cash / avg_weekly_burn)

    return {
        "starting_cash": round(current_cash, 2),
        "forecast_start": forecast_start.isoformat(),
        "forecast": forecast,
        "summary": {
            "lowest_balance": round(lowest_balance, 2),
            "lowest_balance_week": lowest_week,
            "negative_cash_weeks": negative_weeks,
            "total_cash_in_13w": round(total_in, 2),
            "total_cash_out_13w": round(total_out, 2),
            "average_weekly_burn": round(avg_weekly_burn, 2),
            "runway_weeks": runway_weeks,
        },
        "assumptions": {
            "method": "weighted_moving_average_with_seasonality",
            "history_months": len(monthly_data),
            "ar_collections_factored": round(ar_collections, 2),
            "ap_payments_factored": round(ap_payments, 2),
            "seasonality_applied": bool(seasonality),
            # Every fixed haircut/factor this engine applied, with its value
            # and basis label — a consumer never receives a haircutted number
            # without seeing the haircut (AP-FIN-09).
            "parameters": _forecast_parameters(),
        },
        "errors": near_term_errors,
        "data_quality": {
            "is_partial": bool(near_term_errors),
            "unavailable_sections": [err["error_code"] for err in near_term_errors],
        },
        "generated_at": datetime.utcnow().isoformat(),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Helper functions
# ─────────────────────────────────────────────────────────────────────────────

def _find_qbo_summary(node: dict, target_name: str) -> Optional[list]:
    """Recursively search for a Summary ColData matching the target_name."""
    header = node.get("Header") or {}
    col_data = header.get("ColData") or []
    val = col_data[0].get("value", "") if len(col_data) > 0 and isinstance(col_data[0], dict) else ""
    group = node.get("group", "")
    
    if target_name.lower() in str(val).lower() or target_name.lower() in str(group).lower():
        summary = node.get("Summary") or {}
        summary_cols = summary.get("ColData") or []
        if len(summary_cols) >= 2:
            return summary_cols
            
    for child in node.get("Rows", {}).get("Row", []):
        res = _find_qbo_summary(child, target_name)
        if res is not None:
            return res
            
    return None

def _get_current_cash(qbo_client) -> float:
    """Pull current total cash balance from QBO Balance Sheet."""
    try:
        report = qbo_client.get_report("BalanceSheet", {"date_macro": "Today"})
        rows = report.get("Rows", {}).get("Row", [])
        for section in rows:
            res = _find_qbo_summary(section, "Bank")
            if res is not None and len(res) >= 2 and isinstance(res[1], dict):
                try:
                    return float(res[1].get("value", "0"))
                except (ValueError, TypeError, AttributeError):
                    pass
    except Exception as e:
        logger.warning(
            "Could not get current cash from BS: %s",
            redact_to_string(str(e), max_length=500),
        )
        raise RuntimeError("Current cash balance could not be loaded from QuickBooks.") from e

    return 0.0


def _get_monthly_cash_flows(qbo_client) -> List[Dict[str, Any]]:
    """
    Pull 12 months of P&L data and extract revenue (cash-in proxy)
    and total expenses (cash-out proxy).
    """
    try:
        today = date.today()
        start_of_year = date(today.year - 1, today.month, 1)
        end_of_period = date(today.year, today.month, 1) - timedelta(days=1)

        report = qbo_client.get_report(
            "ProfitAndLoss",
            {
                "start_date": start_of_year.isoformat(),
                "end_date": end_of_period.isoformat(),
                "summarize_column_by": "Month",
            },
        )

        columns = report.get("Columns", {}).get("Column", [])
        data_columns = [
            c for c in columns
            if c.get("ColType") == "Money" and "Total" not in c.get("ColTitle", "")
        ]

        month_labels = [c.get("ColTitle", "") for c in data_columns]
        rows = report.get("Rows", {}).get("Row", [])

        revenue_values = [0.0] * len(data_columns)
        expense_values = [0.0] * len(data_columns)

        income_cols = None
        expense_cols = None
        for section in rows:
            if not income_cols:
                income_cols = _find_qbo_summary(section, "Income")
            if not expense_cols:
                expense_cols = _find_qbo_summary(section, "Expense")

        if income_cols:
            for i in range(len(data_columns)):
                try:
                    revenue_values[i] = abs(float(income_cols[i + 1].get("value", "0")))
                except (IndexError, ValueError, TypeError, AttributeError):
                    pass

        if expense_cols:
            for i in range(len(data_columns)):
                try:
                    expense_values[i] = abs(float(expense_cols[i + 1].get("value", "0")))
                except (IndexError, ValueError, TypeError, AttributeError):
                    pass

        monthly_data = []
        for i, label in enumerate(month_labels):
            monthly_data.append({
                "month": label,
                "month_index": i,
                "revenue": revenue_values[i],
                "expenses": expense_values[i],
                "net": revenue_values[i] - expense_values[i],
            })

        return monthly_data

    except Exception as e:
        logger.warning(
            "Could not get monthly cash flows: %s",
            redact_to_string(str(e), max_length=500),
        )
        raise RuntimeError("Monthly cash-flow history could not be loaded from QuickBooks.") from e


def _compute_weekly_patterns(monthly_data: List[Dict]) -> tuple:
    """
    Compute weighted average weekly cash-in and cash-out.
    More recent months get higher weight.
    """
    if not monthly_data:
        raise RuntimeError("Monthly cash-flow history is unavailable.")

    # Assign weights: most recent month gets highest weight
    n = len(monthly_data)
    weights = [(i + 1) for i in range(n)]  # 1, 2, 3... n
    total_weight = sum(weights)

    weighted_revenue = sum(
        m["revenue"] * w for m, w in zip(monthly_data, weights)
    ) / total_weight
    weighted_expense = sum(
        m["expenses"] * w for m, w in zip(monthly_data, weights)
    ) / total_weight

    # Convert monthly to weekly (see WEEKS_PER_MONTH: the legacy 4.33
    # approximation, intentionally NOT 52/12 — output-number stability)
    weekly_in = weighted_revenue / WEEKS_PER_MONTH
    weekly_out = weighted_expense / WEEKS_PER_MONTH

    return round(weekly_in, 2), round(weekly_out, 2)


def _compute_seasonality(
    monthly_data: List[Dict], today: date
) -> Dict[int, float]:
    """
    Compute a seasonality factor per month.
    Compare each month's revenue to the overall average.
    Returns dict of {month_number: factor} where 1.0 = average.
    """
    if len(monthly_data) < SEASONALITY_MIN_MONTHS:
        return {}  # Not enough data for seasonality

    # Group by calendar month
    month_revenues: Dict[int, List[float]] = {}
    for m in monthly_data:
        label = m.get("month", "")
        # Parse month label (e.g. "Jan 2026" → month 1)
        try:
            parsed = datetime.strptime(label, "%b %Y")
            month_num = parsed.month
        except (ValueError, TypeError):
            continue

        if m["revenue"] > 0:
            if month_num not in month_revenues:
                month_revenues[month_num] = []
            month_revenues[month_num].append(m["revenue"])

    if not month_revenues:
        return {}

    # Overall average
    all_revenues = [v for vals in month_revenues.values() for v in vals]
    overall_avg = statistics.mean(all_revenues) if all_revenues else 1.0

    # Per-month factor
    factors = {}
    for month_num, values in month_revenues.items():
        month_avg = statistics.mean(values)
        factors[month_num] = round(month_avg / overall_avg, 3) if overall_avg else 1.0

    return factors


def _estimate_ar_ap_cash_flows(qbo_client) -> tuple[float, float, List[Dict[str, Any]]]:
    """
    Estimate near-term cash flows from AR and AP aging.
    Returns (expected_collections, expected_payments, errors) over the
    NEAR_TERM_WEEKS window, haircutting the current bucket by
    AR_COLLECTION_HAIRCUT / AP_PAYMENT_HAIRCUT (heuristic defaults, surfaced
    in the payload's assumptions.parameters block).
    """
    ar_collections = 0.0
    ap_payments = 0.0
    errors: List[Dict[str, Any]] = []

    # AR: Pull Aged Receivables
    try:
        ar_report = qbo_client.get_report("AgedReceivables", {"aging_period": AGING_PERIOD_DAYS})
        rows = ar_report.get("Rows", {}).get("Row", [])

        for row in rows:
            col_data = row.get("ColData", [])
            # Customer rows have ColData directly. Skip the Grand Total row.
            val_0 = str(col_data[0].get("value", "")).lower() if col_data and isinstance(col_data[0], dict) else ""
            if col_data and len(col_data) >= 3 and "total" not in val_0:
                try:
                    current_ar = float(col_data[1].get("value", "0"))
                    # Heuristic assumed collection rate — NOT company-calibrated;
                    # surfaced in the payload's assumptions.parameters block.
                    ar_collections += current_ar * AR_COLLECTION_HAIRCUT
                except (ValueError, TypeError, IndexError, AttributeError):
                    pass
    except Exception as e:
        errors.append(
            safe_error_detail(
                error_code="predictive_cash_ar_aging_unavailable",
                user_message="AR aging could not be loaded for near-term cash collections.",
                exc=e,
                retryable=True,
                log_context="predictive_cash_ar_aging_unavailable",
            )
        )

    # AP: Pull Aged Payables
    try:
        ap_report = qbo_client.get_report("AgedPayables", {"aging_period": AGING_PERIOD_DAYS})
        rows = ap_report.get("Rows", {}).get("Row", [])

        for row in rows:
            col_data = row.get("ColData", [])
            # Vendor rows have ColData directly. Skip the Grand Total row.
            val_0 = str(col_data[0].get("value", "")).lower() if col_data and isinstance(col_data[0], dict) else ""
            if col_data and len(col_data) >= 3 and "total" not in val_0:
                try:
                    current_ap = float(col_data[1].get("value", "0"))
                    # Heuristic assumed payment rate — NOT company-calibrated;
                    # surfaced in the payload's assumptions.parameters block.
                    ap_payments += current_ap * AP_PAYMENT_HAIRCUT
                except (ValueError, TypeError, IndexError, AttributeError):
                    pass
    except Exception as e:
        errors.append(
            safe_error_detail(
                error_code="predictive_cash_ap_aging_unavailable",
                user_message="AP aging could not be loaded for near-term cash payments.",
                exc=e,
                retryable=True,
                log_context="predictive_cash_ap_aging_unavailable",
            )
        )

    return ar_collections, ap_payments, errors

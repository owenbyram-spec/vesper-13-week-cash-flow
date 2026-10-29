"""
app/analysis/ap_aging.py
─────────────────────────
Fetches AP aging from QBO AgedPayables / AgedPayableDetail reports.
Mirrors the ar_aging.py pattern exactly.

Returns:
    {
        "as_of": "2026-03-27",
        "buckets": {"current": ..., "1-30": ..., "31-60": ..., "61-90": ..., "90+": ...},
        "total_ap": 5678.90,
        "bills": [{"vendor": "Acme Corp", "balance": 1234.56,
                   "buckets": {"current": ..., "1-30": ..., ...}}, ...]
    }
"""
from __future__ import annotations

import logging
from datetime import date
from ..qbo_client import QBOClient
from ..services.redaction import redact_to_string

logger = logging.getLogger(__name__)


def ap_aging(qbo_client: QBOClient, as_of_date: "date | None" = None, limit: int = 2000):
    """
    Fetches AP aging from the QBO AgedPayables report.
    Falls back to summary if detail report is unavailable.
    """
    # Validate at the source — mirrors ar_aging.py. A non-date as_of_date (e.g.
    # an int) must fail LOUD here, not 20 lines downstream as a cryptic
    # "'int' object has no attribute 'isoformat'" from the QBO param builder.
    if as_of_date is not None and (isinstance(as_of_date, bool) or not isinstance(as_of_date, date)):
        raise TypeError(
            f"ap_aging() as_of_date must be a date, got {type(as_of_date).__name__} ({as_of_date!r})"
        )
    today = as_of_date or date.today()

    try:
        report = qbo_client.get_report(
            "AgedPayables",
            {
                "as_of_date": today.isoformat(),
                "aging_period": "30",
                "num_periods": "4",
                # AP aging is an ACCRUAL concept: on the company's default (often cash)
                # basis QBO returns an all-zero report even when real payables exist.
                # Force accrual — matches the AgedPayableDetail fallback + the GL report.
                "accounting_method": "Accrual",
            },
        )
    except Exception:
        # Fallback to detail report
        try:
            report = qbo_client.get_report(
                "AgedPayableDetail",
                {
                    "end_date": today.isoformat(), # detail reports use date ranges
                    "aging_period": "30",
                    "num_periods": "4",
                    "accounting_method": "Accrual",
                },
            )
        except Exception as e:
            logger.warning(
                "[ap_aging] Both AgedPayables and AgedPayableDetail failed: %s",
                redact_to_string(str(e), max_length=500),
            )
            raise RuntimeError("AP aging data could not be loaded from QuickBooks.") from e

    # Dedicated AgedPayables parser (mirrors ar_aging). The P&L/BS/GL section walk lives
    # in app/budget/qbo_reports.py; the aging report is a flat vendor-row + aging-bucket
    # shape, parsed here. Each bucket stays DISTINCT (Current and 1-30 are never folded);
    # each vendor's balance comes from QBO's own Total column, and each vendor row keeps
    # its own per-bucket breakdown for the aging grid.
    cols = report.get("Columns", {}).get("Column", [])  # QBO-OK: canonical-parser (dedicated aging-report parser)
    col_titles = [c.get("ColTitle", "") or c.get("colTitle", "") for c in cols]
    col_types  = [c.get("ColType", "")  or c.get("colType", "")  for c in cols]

    logger.info(f"[ap_aging] col_titles={col_titles} | col_types={col_types}")

    # Bucket order matches the displayed grid: current | 1-30 | 31-60 | 61-90 | 90+.
    bucket_cols: dict[str, list[int]] = {"current": [], "1-30": [], "31-60": [], "61-90": [], "90+": []}
    total_col_idx: "int | None" = None
    for i, title in enumerate(col_titles):
        if i == 0:
            continue  # name / vendor column
        t = title.lower().replace(" ", "").replace(",", "")
        if not t:
            continue
        if t == "total" or t.endswith("total"):
            total_col_idx = i
            continue
        # Current and 1-30 are DISTINCT buckets (match 1-30 before the bare "current"
        # catch so a "current" substring can't swallow a dated column).
        if any(x in t for x in ("1-30", "0-30", "030", "130")):
            bucket_cols["1-30"].append(i)
        elif "current" in t:
            bucket_cols["current"].append(i)
        elif any(x in t for x in ("31-60", "3160", "31")):
            bucket_cols["31-60"].append(i)
        elif any(x in t for x in ("61-90", "6190", "61")):
            bucket_cols["61-90"].append(i)
        elif any(x in t for x in (">90", "91", "90+", "over90", "90andup", "91andover")):
            bucket_cols["90+"].append(i)

    any_bucket_cols = any(bucket_cols.values())

    buckets: dict = {"current": 0.0, "1-30": 0.0, "31-60": 0.0, "61-90": 0.0, "90+": 0.0}
    total_ap = 0.0
    bills = []

    def _extract(cell: dict) -> float:
        try:
            return float(cell.get("value", "0") or 0)
        except (TypeError, ValueError):
            return 0.0

    def _walk_rows(rows: list) -> None:
        nonlocal total_ap
        for row in rows:
            rtype = row.get("type", "")  # SA-OK: dict access on QBO report JSON, no DB session in this module
            if rtype == "Section":
                # The only Section in an aging report is the grand-total row (no nested
                # Rows), so recursing is a safety net, not a double-count.
                _walk_rows(row.get("Rows", {}).get("Row", []))  # SA-OK: dict access, no DB; QBO-OK: canonical-parser
                continue
            if rtype == "Summary":
                continue
            cells = row.get("ColData")  # SA-OK: dict access on QBO report JSON, no DB session in this module
            if not cells:
                continue
            # QBO OMITS "type" on vendor data rows (ColData only), so the old
            # `type == "Data"` check skipped every one — the all-zero-aging root cause.
            # Compute THIS row's per-bucket split first; the grand-total buckets are
            # accumulated below ONLY for rows we also keep in `bills`, so the summary
            # bucket totals equal Σ(per-vendor buckets) by construction (reconciliation
            # invariant — plan 20260712-aging-single-source).
            row_buckets: dict = {"current": 0.0, "1-30": 0.0, "31-60": 0.0, "61-90": 0.0, "90+": 0.0}
            for bkt, idxs in bucket_cols.items():
                for idx in idxs:
                    if idx < len(cells):
                        row_buckets[bkt] += _extract(cells[idx])
            vendor = cells[0].get("value", "") if cells else ""  # SA-OK: dict access on report JSON, no DB session
            if total_col_idx is not None and total_col_idx < len(cells):
                row_total = _extract(cells[total_col_idx])
            else:
                row_total = sum(row_buckets.values())
            # Keep a row iff it carries ANY activity. A vendor whose Total nets to 0 but
            # has offsetting bucket cells (e.g. +X Current, −X 90+ — a credit against an
            # open bill) is a REAL QBO row: it must contribute to BOTH the grand-total
            # buckets AND the per-vendor list, or the summary split desyncs from the rows.
            if row_total != 0 or any(v != 0 for v in row_buckets.values()):
                for bkt in buckets:
                    buckets[bkt] += row_buckets[bkt]
                total_ap += row_total
                bills.append({"vendor": vendor, "balance": row_total, "buckets": row_buckets})

    _walk_rows(report.get("Rows", {}).get("Row", []))  # QBO-OK: canonical-parser (dedicated aging-report parser)

    # Fail loud on an UNREADABLE report rather than returning silent zeros (mirrors
    # ar_aging): no aging columns matched AND nothing parsed (e.g. the AgedPayableDetail
    # fallback, whose transaction-level columns carry no aging buckets). Raising lets the
    # gatherer record report_ap_aging_unavailable so the renderer shows "temporarily
    # unavailable", not a fabricated all-zero AP. A genuine zero-AP company still returns
    # bucket columns.
    if not any_bucket_cols and total_ap == 0.0 and not bills:
        logger.warning("[ap_aging] no bucket columns and no rows parsed — report shape unreadable")
        raise RuntimeError("AP aging report returned an unreadable column layout.")

    logger.info(f"[ap_aging] bucket_cols={bucket_cols} | buckets={buckets} | total_ap={total_ap:.0f}")

    return {
        "as_of": today.isoformat(),
        "buckets": buckets,
        "total_ap": total_ap,
        "bills": bills,
    }

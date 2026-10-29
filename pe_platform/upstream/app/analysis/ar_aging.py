from __future__ import annotations

import logging
from datetime import date
from ..qbo_client import QBOClient
from ..services.redaction import redact_to_string

logger = logging.getLogger(__name__)


def ar_aging(qbo_client: QBOClient, as_of_date: "date | None" = None, limit: int = 2000):
    """
    Fetches AR aging from the QBO AgedReceivables report (not the invoice query,
    which returns a 400 error for some plans/companies).

    The AgedReceivables report returns rows per customer with aging bucket columns.
    """
    # Validate at the source: a caller-supplied as_of_date that is not a real
    # date (e.g. an int — a year, an epoch timestamp, or a positional-arg
    # transposition with `limit`) must fail LOUD and CLEAR here, not surface
    # 20+ lines downstream as a cryptic "'int' object has no attribute
    # 'isoformat'" once it reaches the QBO report param builder. `bool` is
    # rejected too — it is an `int` subclass in Python, never a valid date.
    if as_of_date is not None and (isinstance(as_of_date, bool) or not isinstance(as_of_date, date)):
        raise TypeError(
            f"ar_aging() as_of_date must be a date, got {type(as_of_date).__name__} ({as_of_date!r})"
        )
    today = as_of_date or date.today()

    try:
        report = qbo_client.get_report(
            "AgedReceivables",
            {
                "as_of_date": today.isoformat(),
                "aging_period": "30",
                "num_periods": "4",
                # AR aging is an ACCRUAL concept: on the company's default (often cash)
                # basis QBO returns an all-zero report even when real receivables exist.
                # Force accrual — matches the AgedReceivableDetail fallback + the GL report.
                "accounting_method": "Accrual",
            },
        )
    except Exception:
        # Fallback to detail report
        try:
            report = qbo_client.get_report(
                "AgedReceivableDetail",
                {
                    "end_date": today.isoformat(),
                    "aging_period": "30",
                    "num_periods": "4",
                    "accounting_method": "Accrual",
                },
            )
        except Exception as e:
            logger.warning(
                "[ar_aging] Both AgedReceivables and AgedReceivableDetail failed: %s",
                redact_to_string(str(e), max_length=500),
            )
            raise RuntimeError("AR aging data could not be loaded from QuickBooks.") from e

    # Dedicated AgedReceivables parser. The P&L/BS/GL section walk lives in
    # app/budget/qbo_reports.py; the aging report is a different shape (flat customer
    # rows + aging-bucket columns), so it is parsed here. QBO returns one name column,
    # the aging buckets (Current / 1-30 / 31-60 / 61-90 / 91+), and a trailing Total.
    # We keep each bucket DISTINCT — Current and 1-30 are their own columns, never
    # folded — and use the Total column for each customer's balance. Every customer row
    # keeps its own per-bucket breakdown so the UI can render a full aging grid.
    cols = report.get("Columns", {}).get("Column", [])  # QBO-OK: canonical-parser (dedicated aging-report parser)
    col_titles = [c.get("ColTitle", "") or c.get("colTitle", "") for c in cols]
    col_types  = [c.get("ColType", "")  or c.get("colType", "")  for c in cols]

    logger.info(f"[ar_aging] col_titles={col_titles} | col_types={col_types}")

    # Bucket order matches the displayed grid: current | 1-30 | 31-60 | 61-90 | 90+.
    bucket_cols: dict[str, list[int]] = {"current": [], "1-30": [], "31-60": [], "61-90": [], "90+": []}
    total_col_idx: "int | None" = None
    for i, title in enumerate(col_titles):
        if i == 0:
            continue  # name / customer column
        t = title.lower().replace(" ", "").replace(",", "")
        if not t:
            continue
        if t == "total" or t.endswith("total"):
            total_col_idx = i
            continue
        # Current and 1-30 are now DISTINCT buckets (order matters: match 1-30 before the
        # bare "current" catch so a "current" substring can't swallow a dated column).
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
    total_ar = 0.0
    invoices = []

    def _extract(cell: dict) -> float:
        try:
            return float(cell.get("value", "0") or 0)
        except (TypeError, ValueError):
            return 0.0

    def _walk_rows(rows: list) -> None:
        nonlocal total_ar
        for row in rows:
            rtype = row.get("type", "")  # SA-OK: dict access on QBO report JSON, no DB session in this module
            if rtype == "Section":
                # The only Section in an aging report is the grand-total row (it has no
                # nested Rows), so recursing is a safety net, not a double-count.
                _walk_rows(row.get("Rows", {}).get("Row", []))  # SA-OK: dict access, no DB; QBO-OK: canonical-parser
                continue
            if rtype == "Summary":
                continue
            cells = row.get("ColData")  # SA-OK: dict access on QBO report JSON, no DB session in this module
            if not cells:
                continue
            # QBO OMITS "type" on customer data rows (they carry only ColData), so the old
            # `type == "Data"` check skipped every one of them — the all-zero-aging root
            # cause. Treat any ColData-bearing, non-summary row as a customer data row.
            # Compute THIS row's per-bucket split first; the grand-total buckets are
            # accumulated below ONLY for rows we also keep in `invoices`, so the summary
            # bucket totals equal Σ(per-customer buckets) by construction (reconciliation
            # invariant — plan 20260712-aging-single-source).
            row_buckets: dict = {"current": 0.0, "1-30": 0.0, "31-60": 0.0, "61-90": 0.0, "90+": 0.0}
            for bkt, idxs in bucket_cols.items():
                for idx in idxs:
                    if idx < len(cells):
                        row_buckets[bkt] += _extract(cells[idx])
            customer = cells[0].get("value", "") if cells else ""  # SA-OK: dict access on report JSON, no DB session
            # Balance from QBO's own Total column (includes Current); fall back to the sum
            # of the bucket columns only if no Total column was identified.
            if total_col_idx is not None and total_col_idx < len(cells):
                row_total = _extract(cells[total_col_idx])
            else:
                row_total = sum(row_buckets.values())
            # Keep a row iff it carries ANY activity. A customer whose Total nets to 0 but
            # has offsetting bucket cells (e.g. +X Current, −X 90+ — a credit memo against
            # an open invoice) is a REAL QBO row: it must contribute to BOTH the grand-total
            # buckets AND the per-customer list, or the summary split desyncs from the rows.
            if row_total != 0 or any(v != 0 for v in row_buckets.values()):
                for bkt in buckets:
                    buckets[bkt] += row_buckets[bkt]
                total_ar += row_total
                invoices.append({"customer": customer, "balance": row_total, "buckets": row_buckets})

    _walk_rows(report.get("Rows", {}).get("Row", []))  # QBO-OK: canonical-parser (dedicated aging-report parser)

    # Fail loud on an UNREADABLE report rather than returning silent zeros: no aging
    # columns matched AND nothing parsed (e.g. the AgedReceivableDetail fallback, whose
    # transaction-level columns carry no aging buckets). Raising lets the gatherer record
    # report_ar_aging_unavailable so the renderer shows "temporarily unavailable" instead
    # of a fabricated all-zero AR. A genuine zero-AR company still returns bucket columns.
    if not any_bucket_cols and total_ar == 0.0 and not invoices:
        logger.warning("[ar_aging] no bucket columns and no rows parsed — report shape unreadable")
        raise RuntimeError("AR aging report returned an unreadable column layout.")

    logger.info(f"[ar_aging] bucket_cols={bucket_cols} | buckets={buckets} | total_ar={total_ar:.0f}")

    return {
        "as_of": today.isoformat(),
        "buckets": buckets,
        "total_ar": total_ar,
        "invoices": invoices,
    }

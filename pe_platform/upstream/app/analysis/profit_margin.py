from __future__ import annotations

import calendar
import logging
from datetime import date, timedelta
from ..qbo_client import QBOClient

logger = logging.getLogger(__name__)

# ── Label sets ────────────────────────────────────────────────────────────────
_TOTAL_INCOME_LABELS = {
    "total income", "total revenue", "total sales", "total gross receipts",
    "total service revenue", "total other income",
}
_COGS_TOTAL_LABELS = {
    "total cost of goods sold", "total cogs", "total cost of sales",
    "total direct costs", "total direct labor", "total direct expenses",
}
_GROSS_PROFIT_LABELS = {
    "gross profit",
}
_NET_INCOME_LABELS = {
    "net income", "net loss", "net income (loss)", "net profit",
    "net operating income", "net ordinary income",
}
_INCOME_SECTION_KW = {"income", "revenue", "sales", "gross receipts"}
_COGS_SECTION_KW   = {"cost of goods sold", "cogs", "cost of sales",
                      "direct costs", "direct labor", "direct expenses"}


def _extract_amount(cell: dict) -> float:
    try:
        return float(cell.get("value", "0") or 0)
    except Exception:
        return 0.0


def _is_total_col(label: str) -> bool:
    l = label.strip().lower()  # noqa: E741
    if l in ("total", ""):
        return True
    if "," in l:
        return True
    return False


def _parse_qbo_month(label: str) -> str:
    _ABBR = {
        "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
        "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    }
    parts = label.strip().split()
    if len(parts) == 2:
        abbr = parts[0][:3].lower()
        year = parts[1]
        mo = _ABBR.get(abbr)
        if mo and year.isdigit() and len(year) == 4:
            return f"{year}-{mo:02d}"
    return ""


def _cells_to_vals(cells: list, n: int, skip_first: bool = True) -> list:
    """Convert a ColData list to a float array of length n."""
    vals = [0.0] * n
    src = cells[1:] if skip_first else cells
    for i, cell in enumerate(src):
        if i < n:
            vals[i] = _extract_amount(cell if isinstance(cell, dict) else {})
    return vals


def _add(target: list, source: list) -> None:
    for i in range(min(len(target), len(source))):
        target[i] += source[i]


def _lbl(cells: list) -> str:
    if cells:
        return (cells[0].get("value", "") or "").lower().strip()
    return ""


def _matches(text: str, label_set: set) -> bool:
    return any(t in text for t in label_set)


def _last_day_of_month(d: date) -> date:
    """Return the last calendar day of d's month."""
    return d.replace(day=calendar.monthrange(d.year, d.month)[1])


def _last_closed_month_end() -> date:
    """Return the last day of the most recently completed calendar month."""
    today = date.today()
    first_of_this_month = today.replace(day=1)
    last_month_end = first_of_this_month - timedelta(days=1)
    return last_month_end


def profit_and_margin_by_month(qbo_client: QBOClient, as_of_date: "date | None" = None):
    """
    Reads the QBO ProfitAndLoss monthly report and extracts income, COGS,
    gross profit, and net income per month.

    QBO uses INCONSISTENT row types across plans:
      • Total Income / Total COGS may be in a Summary-type sub-row OR in a
        Section's .Summary dict property.
      • Gross Profit / Net Income may be standalone Data rows OR Section rows
        where the dollar amounts live in Header.ColData.

    Strategy: walk EVERY part of the response tree and classify by label,
    reading values from ColData (Data/Summary rows) AND from Header.ColData
    (Section headers that carry their own dollar amounts).

    as_of_date: if given, fetches 12 months ending on the last day of that
                month (e.g. date(2026,1,31) → Feb 2025 – Jan 2026). Defaults
                to the last fully-completed calendar month.
    """
    if as_of_date is None:
        as_of_date = _last_closed_month_end()
    # Snap to last day of the given month
    end_date = _last_day_of_month(as_of_date)
    # TTM = 12 full months back: first day of month 12 months prior
    start_date = end_date.replace(day=1)
    # Go back 11 more months
    m = start_date.month - 11
    y = start_date.year
    if m <= 0:
        m += 12
        y -= 1
    start_date = date(y, m, 1)
    logger.info(f"[profit_margin] TTM range: {start_date} → {end_date}")

    report = qbo_client.get_report(
        "ProfitAndLoss",
        {
            "summarize_column_by": "Month",
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "accounting_method": "Accrual",
        },
    )

    cols = report.get("Columns", {}).get("Column", [])
    col_keys = [c.get("ColTitle", "") for c in cols]
    col_types = [c.get("ColType", "") or c.get("colType", "") for c in cols]
    n = len(col_keys)

    if n == 0:
        logger.warning("[profit_margin] ProfitAndLoss returned 0 columns")
        raise RuntimeError("Profit and margin data could not be parsed from QuickBooks report columns.")

    # Build a set of column indices that are TOTAL columns:
    # 1. QBO marks them with ColType="TOTAL"
    # 2. The label "Total" / "" / comma-containing date range
    # 3. Dedup: any month key already seen from an earlier column
    total_col_indices: set[int] = set()
    seen_month_keys: set[str] = set()
    for i, (title, ctype) in enumerate(zip(col_keys, col_types)):
        if ctype and ctype.upper() in ("TOTAL", "SUBTOTAL"):
            total_col_indices.add(i)
            continue
        if _is_total_col(title):
            total_col_indices.add(i)
            continue
        key = _parse_qbo_month(title)
        if key:
            if key in seen_month_keys:
                # Duplicate month key → this is the Total column mislabeled
                total_col_indices.add(i)
            else:
                seen_month_keys.add(key)

    logger.info(f"[profit_margin] cols={col_keys} | total_col_indices={total_col_indices}")


    income_total    = [0.0] * n
    income_fallback = [0.0] * n   # sum of Data rows inside income section
    cogs            = [0.0] * n
    gross_profit    = [0.0] * n
    net_income      = [0.0] * n

    seen_labels: set[str] = set()   # tracks which classified labels we've already accumulated
    debug_labels: list[str] = []

    def _process_coldata_row(cells: list, row_type: str, in_income: bool, in_cogs: bool) -> None:
        """Classify and accumulate a row identified by its ColData cells."""
        label = _lbl(cells)
        if not label:
            return
        debug_labels.append(f"[{row_type}] {label}")
        vals = _cells_to_vals(cells, n)

        if _matches(label, _NET_INCOME_LABELS):
            if label not in seen_labels:
                _add(net_income, vals)
                seen_labels.add(label)
        elif _matches(label, _GROSS_PROFIT_LABELS):
            if label not in seen_labels:
                _add(gross_profit, vals)
                seen_labels.add(label)
        elif _matches(label, _TOTAL_INCOME_LABELS):
            if label not in seen_labels:
                _add(income_total, vals)
                seen_labels.add(label)
        elif _matches(label, _COGS_TOTAL_LABELS):
            if label not in seen_labels:
                _add(cogs, vals)
                seen_labels.add(label)
        elif in_income and row_type in ("Data", "data") and sum(vals) > 0:
            _add(income_fallback, vals)
        elif in_cogs and row_type in ("Data", "data") and sum(vals) > 0:
            _add(cogs, vals)

    def _is_income_section(hdr: str) -> bool:
        return (
            _matches(hdr, _INCOME_SECTION_KW)
            and not _matches(hdr, _COGS_SECTION_KW | {"expense", "operating", "other"})
        )

    def _is_cogs_section(hdr: str) -> bool:
        return _matches(hdr, _COGS_SECTION_KW)

    def _scan(rows: list, in_income: bool, in_cogs: bool) -> None:
        for row in rows:
            rtype = row.get("type", "")

            if rtype == "Section":
                hdr_cells = row.get("Header", {}).get("ColData", [])
                hdr_lbl   = _lbl(hdr_cells)

                # ── 1. Section headers that carry their own dollar amounts ──
                # e.g. QBO puts "Net Income" or "Gross Profit" as a Section
                # whose dollar values are in the Header.ColData columns.
                if len(hdr_cells) > 1:
                    _process_coldata_row(hdr_cells, f"Section-header:{rtype}", in_income, in_cogs)

                # ── 2. Section-level Summary dict (separate from sub-rows) ──
                # QBO sometimes stores the section total as a .Summary property.
                # NOTE: Only read this if the same label has NOT already been
                # captured via sub-rows (prevent double-count).
                section_summary = row.get("Summary", {})
                if isinstance(section_summary, dict) and section_summary.get("ColData"):
                    ss_cells = section_summary["ColData"]
                    ss_label = _lbl(ss_cells)
                    if ss_label and ss_label not in seen_labels:
                        _process_coldata_row(ss_cells, "Section-Summary", in_income, in_cogs)

                # ── 3. Recurse into sub-rows ───────────────────────────────
                this_income = in_income or _is_income_section(hdr_lbl)
                this_cogs   = in_cogs   or _is_cogs_section(hdr_lbl)
                _scan(row.get("Rows", {}).get("Row", []), this_income, this_cogs)

            else:
                # Data, Summary, GrandTotal, or any other type
                cells = row.get("ColData", [])
                _process_coldata_row(cells, rtype, in_income, in_cogs)

    _scan(report.get("Rows", {}).get("Row", []), False, False)

    # Also check report-level GrandTotal if present
    gt = report.get("Rows", {}).get("GrandTotal", {})
    if isinstance(gt, dict) and gt.get("ColData"):
        gt_label = _lbl(gt["ColData"])
        if gt_label not in seen_labels:
            _process_coldata_row(gt["ColData"], "GrandTotal", False, False)

    # Choose income: prefer the "Total Income" summary row; fallback to Data rows
    income_by_month = income_total if sum(income_total) != 0 else income_fallback

    # Derive gross profit: prefer QBO-labelled row; fall back to income − cogs
    use_gp = (
        gross_profit
        if sum(gross_profit) != 0
        else [income_by_month[i] - cogs[i] for i in range(n)]
    )

    logger.info(
        f"[profit_margin] labels={debug_labels} | "
        f"income_total={sum(income_total):.0f} | income_fallback={sum(income_fallback):.0f} | "
        f"cogs={sum(cogs):.0f} | gross_profit={sum(gross_profit):.0f} | "
        f"net_income={sum(net_income):.0f}"
    )

    result = []
    result_month_keys: set[str] = set()
    for i, month_label in enumerate(col_keys):
        if i in total_col_indices:
            continue
        key = _parse_qbo_month(month_label)
        if not key:
            continue
        if key in result_month_keys:
            continue   # extra safety: skip duplicate month keys
        result_month_keys.add(key)

        inc   = income_by_month[i]
        c     = cogs[i]
        gp    = use_gp[i]
        ni    = net_income[i]
        ebitda = ni if ni != 0.0 else gp

        result.append({
            "month":            key,
            "income":           inc,
            "cogs":             c,
            "gross_profit":     gp,
            "gross_margin_pct": (gp / inc * 100.0) if inc else 0.0,
            "net_income":       ni,
            "ebitda":           ebitda,
        })

    total_income = sum(m["income"]       for m in result)
    total_cogs   = sum(m["cogs"]         for m in result)
    total_gp     = sum(m["gross_profit"] for m in result)
    total_net    = sum(m["net_income"]   for m in result)
    total_ebitda = sum(m["ebitda"]       for m in result)

    return {
        "months":             result,
        "total_income":       total_income,
        "total_cogs":         total_cogs,
        "total_gross_profit": total_gp,
        "total_net_income":   total_net,
        "total_ebitda":       total_ebitda,
        "_debug_labels":      debug_labels,
    }

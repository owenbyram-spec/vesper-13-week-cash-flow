# PLAN-EXEMPT: covenant monthly-grid row-plan under ratified plan 20260630-covenant-redesign; the shared DISPLAY-ONLY row builder that both the Excel and PDF monthly-grid renderers consume, factored out so neither renderer file nears the 500-line cap.
"""
app/reporting/covenant_monthly_rows.py
────────────────────────────────────────
The SHARED row-plan for the bank-send monthly covenant grid — the ONE place that maps a
``CovenantSeries`` dict onto the exact row order the bank workbook
(``work/covenant-redesign/design/Covenant-Examples-TARGET.xlsx``) uses, so the Excel and
PDF renderers stay pixel-identical to each other and to the workbook.

DISPLAY-ONLY (AP-DR-06): every value here is READ from the pre-computed series dict
(``compute_monthly_series(...).to_dict()``). The only arithmetic is the summary ("Total" /
"Amount as Tested") column, which SUMS the already-computed monthly flows (the workbook's
``=SUM(C:N)``) or SELECTS the already-computed TTM value / latest balance — it re-derives NO
covenant metric. A month with ``no_data`` (or a series that is not ``available``) yields a
``None`` cell the renderers show as an em-dash + reason, never a fabricated 0.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any  # CRAFT-OK: Any = dynamic covenant-series dict boundary

Payload = dict[str, Any]  # CRAFT-OK: dynamic provider-derived covenant-series payload boundary

# Cell kinds drive how the renderer formats a value:
#   "money"  → currency (whole dollars, accounting format)
#   "ratio"  → a multiple (e.g. 2.2000), 4 decimals like the workbook
#   "text"   → a literal label (only used for the Covenant Amount threshold display)
KIND_MONEY = "money"
KIND_RATIO = "ratio"


@dataclass
class GridRow:
    """One display row of the grid: a label, its 12 monthly cells, and the summary cell.
    ``kind`` tells the renderer how to format every numeric cell in the row. ``emphasis``
    flags the ratio/covenant/tested rows the renderer bolds (mirrors the workbook)."""
    label: str
    kind: str
    months: list[float | None]
    summary: float | None
    emphasis: bool = False


def _sum(values: list[float | None]) -> float | None:
    present = [v for v in values if v is not None]
    if not present:
        return None
    return round(sum(present), 2)


def _months(series: Payload) -> list[Payload]:
    return list(series.get("months", []) or [])


def _col(series: Payload, key: str) -> list[float | None]:
    """The per-month value of one CovenantMonth field, oldest→newest (display-only read)."""
    return [m.get(key) for m in _months(series)]  # SA-OK: in-memory series-dict read, not a DB Session


def month_labels(series: Payload) -> list[str]:
    """The 12 month-column headers = the CovenantMonth ``period`` titles (verbatim)."""
    return [str(m.get("period", "")) for m in _months(series)]  # SA-OK: in-memory series-dict read, no DB Session


def _latest_present(values: list[float | None]) -> float | None:
    for v in reversed(values):
        if v is not None:
            return v
    return None


def dscr_rows(series: Payload, threshold: float | None) -> list[GridRow]:
    """DSCR sheet row order (AC-1): Interest, Principal, Total Debt Service, Net Income,
    Interest (repeat), D&A, EBITDA, Debt Service Coverage Ratio (run-rate), Overage,
    Covenant Amount, TTM DSCR. Summary column = Σ flows / ratio=TTM."""
    interest = _col(series, "interest")
    principal = _col(series, "principal")
    debt_service = _col(series, "debt_service")
    net_income = _col(series, "net_income")
    d_and_a = _col(series, "d_and_a")
    ebitda = _col(series, "ebitda")
    run_rate = _col(series, "run_rate_ratio")
    overage = _col(series, "overage")
    ttm_ratio = series.get("ttm_ratio")
    ttm_overage = series.get("ttm_overage")

    return [
        GridRow("Interest Expense", KIND_MONEY, interest, _sum(interest)),
        GridRow("Principal Payment", KIND_MONEY, principal, _sum(principal)),
        GridRow("Total Debt Service", KIND_MONEY, debt_service, _sum(debt_service), emphasis=True),
        GridRow("Net Income", KIND_MONEY, net_income, _sum(net_income)),
        GridRow("Interest Expense", KIND_MONEY, list(interest), _sum(interest)),
        GridRow("Depreciation and Amortization", KIND_MONEY, d_and_a, _sum(d_and_a)),
        GridRow("EBITDA", KIND_MONEY, ebitda, _sum(ebitda), emphasis=True),
        GridRow("Debt Service Coverage Ratio", KIND_RATIO, run_rate, ttm_ratio, emphasis=True),
        GridRow("Overage (Shortfall) in Covenant", KIND_MONEY, overage, ttm_overage),
        GridRow("Covenant Amount", KIND_RATIO, [threshold], threshold),
        GridRow("TTM DSCR", KIND_RATIO, [ttm_ratio], ttm_ratio, emphasis=True),
    ]


def debt_ebitda_rows(series: Payload, threshold: float | None) -> list[GridRow]:
    """DEBT EBITDA sheet row order (AC-1): Total Debt, Net Income, Interest, D&A, EBITDA,
    Debt to EBITDA (run-rate), Covenant Amount, DEBT TO EBITDA (=TTM). Coverage-only
    Overage is OMITTED (leverage metric, as the workbook does). Summary column: Total
    Debt = latest month-end balance (NOT summed); NI/D&A/EBITDA = Σ; Interest unused;
    ratio = TTM."""
    total_debt = _col(series, "total_debt")
    net_income = _col(series, "net_income")
    interest = _col(series, "interest")
    d_and_a = _col(series, "d_and_a")
    ebitda = _col(series, "ebitda")
    run_rate = _col(series, "run_rate_ratio")
    ttm_ratio = series.get("ttm_ratio")

    return [
        # Total Debt "as tested" = latest month-end balance (a balance, not a sum).
        GridRow("Total Debt", KIND_MONEY, total_debt, _latest_present(total_debt), emphasis=True),
        GridRow("Net Income", KIND_MONEY, net_income, _sum(net_income)),
        # Interest "as tested" is unused for this metric in the workbook — no summary value.
        GridRow("Interest Expense", KIND_MONEY, interest, None),
        GridRow("Depreciation and Amortization", KIND_MONEY, d_and_a, _sum(d_and_a)),
        GridRow("EBITDA", KIND_MONEY, ebitda, _sum(ebitda), emphasis=True),
        GridRow("Debt to EBITDA (Run Rate for Monthly)", KIND_RATIO, run_rate, ttm_ratio, emphasis=True),
        GridRow("Covenant Amount", KIND_RATIO, [threshold], threshold),
        GridRow("DEBT TO EBITDA", KIND_RATIO, [ttm_ratio], ttm_ratio, emphasis=True),
    ]


def rows_for(entry: Payload) -> list[GridRow]:
    """Pick the row-plan for a covenant entry by its lens. A leverage metric
    (DEBT_EBITDA) uses the Debt/EBITDA layout; everything else uses the DSCR layout
    (the blocked-branch rule: mirror the closest sheet, never invent a divergent one)."""
    series = entry.get("series") or {}
    threshold = entry.get("threshold")
    lens = str(series.get("lens", "coverage"))
    if lens == "leverage":
        return debt_ebitda_rows(series, threshold)
    return dscr_rows(series, threshold)


def summary_header(entry: Payload) -> str:
    """The summary-column header: "Amount as Tested" for a leverage metric (a balance-
    based test), "Total" for a coverage metric (a flow sum) — mirrors the workbook."""
    series = entry.get("series") or {}
    return "Amount as Tested" if str(series.get("lens", "coverage")) == "leverage" else "Total"

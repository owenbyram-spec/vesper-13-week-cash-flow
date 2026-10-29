"""
app/services/covenant_compute.py
────────────────────────────────
Two-lens monthly covenant compute engine (plan 20260630-covenant-redesign, Wave 1).

Produces, for one covenant over a 12-month window, a monthly series that reproduces the
bank's covenant workbook (``work/covenant-redesign/design/Covenant-Examples-TARGET.xlsx``):

  • component build-ups per month — Net Income, mapped Interest, mapped D&A → EBITDA
    (EBITDA = Net Income + Σ mapped add-backs), Interest + Principal → Debt Service, Total Debt
  • the RUN-RATE lens (early warning) — the month annualized
  • the rolling-TTM lens (the true default test) — trailing-12 flows / latest balance
  • the ``$ overage`` = EBITDA − (threshold × Debt Service) for coverage (DSCR) covenants

Sourced LIVE from QBO with ONE monthly-columned report each — ProfitAndLoss, BalanceSheet,
and the Statement of Cash Flows (``CashFlow``) — via ``qbo_client.get_report(...,
summarize_column_by="Month")``. Per-month principal is the abs of the financing-section
debt-repayment line from the Cash Flow Statement (NOT liability-balance diffing).

Reuses the canonical QBO report parser (``app/budget/qbo_reports``) and the covenant EBITDA
add-back resolver (``app/services/coa_formula``) — no forked report walk, no new schema.
Honest degradation: a missing input yields a specific reason code, never a fabricated 0.

The account-group mapping is REUSED from existing config (plan DEC-CR-1): interest / D&A come
from the company-level ``CompanyEbitdaConfig.addbacks`` category tags; Total Debt (and the CF
principal scope) is the covenant's ``debt_service_accounts``. No new column.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import date

# Reuse the canonical QBO report parser (AP-QBO-03 — one shared parser, do not fork the walk).
from app.budget.qbo_reports import (  # QBO-OK: canonical-parser
    _parse_amount,
    _parse_pl_single_month_entries,
)
from app.semantics import (
    Basis,
    MetricRef,
    Period,
    PeriodGrain,
    Scenario,
    check_comparable,
    get_metric,
)
from app.services.coa_formula import NOT_MEANINGFUL_REASONS, _resolve_addbacks
from app.services.redaction import redact_to_string

logger = logging.getLogger(__name__)

# Metrics whose covenant is a COVERAGE ratio (flow / flow, breach when BELOW): the run-rate is
# the raw monthly ratio (no ×12 — a ratio of two monthly flows is scale-free), and the workbook
# reports a ``$ overage`` = EBITDA − threshold×DebtService for them.
COVERAGE_METRICS = ("DSCR",)
# Metrics whose covenant is a LEVERAGE ratio (stock / annualized-flow, breach when ABOVE): the
# run-rate annualizes the month's EBITDA (×12), and TTM = latest balance / trailing-12 EBITDA.
LEVERAGE_METRICS = ("DEBT_EBITDA",)
# The metrics the monthly two-lens engine supports. The report + snapshot loops filter to these;
# any other metric type is out of scope for the two-lens monthly grid (it keeps its single-period
# /covenants/status verdict) and the engine degrades it honestly (available=False) if asked.
SUPPORTED_METRICS = COVERAGE_METRICS + LEVERAGE_METRICS
MONTHS_PER_YEAR = 12


@dataclass
class CovenantMonth:
    """One month column of the two-lens grid. ``no_data`` is a reason code (never a fake 0).

    ``data_quality`` (FOS-1 Wave B, AC-1/AC-2) carries structured diagnostics that never
    change a computed number: label-resolution codes (``label_unresolved:<opts>`` /
    ``label_ambiguous:<label>``), month-column join guards (``month_key_missing:<report>:<title>``),
    and semantic-comparability codes from ``app.semantics.check_comparable``
    (``compat:<pair>:<reason>``). Empty list = a clean month."""
    period: str
    net_income: float | None
    interest: float
    taxes: float
    d_and_a: float
    ebitda: float | None
    principal: float | None
    debt_service: float | None
    total_debt: float | None
    run_rate_ratio: float | None
    overage: float | None
    no_data: str | None = None
    data_quality: list[str] = field(default_factory=list)
    # Leverage lens: the month's EBITDA is zero or negative, so its run-rate ratio is not
    # meaningful (None) — a maximum covenant is in breach that month (coa_formula.NOT_MEANINGFUL_REASONS).
    run_rate_not_meaningful: str | None = None


@dataclass
class CovenantSeries:
    """The full 12-month two-lens result for one covenant.

    ``data_quality`` (FOS-1 Wave B) carries series-level diagnostics — the month-column
    join guards (``month_key_ambiguous:<report>:<key>`` / ``month_key_unmatched:<report>:<title>``)
    and the TTM-finalize comparability codes (``compat:ttm:<pair>:<reason>``). Observability
    only — it never changes a computed number."""
    metric_type: str
    threshold: float
    breach_direction: str
    lens: str  # "coverage" | "leverage"
    months: list[CovenantMonth] = field(default_factory=list)
    ttm_ratio: float | None = None          # the "as tested" value (rolling TTM)
    ttm_overage: float | None = None
    available: bool = False
    reason: str | None = None
    data_quality: list[str] = field(default_factory=list)
    # The TTM ratio is not meaningful (e.g. trailing-12 EBITDA <= 0 for Debt/EBITDA): ttm_ratio
    # stays None, ``available`` is True (the verdict is known) and ``reason`` explains it.
    ttm_not_meaningful: str | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


# ─────────────────────────────────────────────────────────────────────────────
# Month-column readers (labeled summary / account-id / CF-line — NOT the depth-0
# account-type classification walk that AP-QBO-03 owns; that stays in qbo_reports).
# Every loop below walks an IN-MEMORY parsed QBO report, never a DB session.
# ─────────────────────────────────────────────────────────────────────────────

def _money_month_columns(report: dict) -> list[tuple[int, str]]:
    """Ordered (column_index, title) for each non-Total Money column of a monthly report."""
    cols = report.get("Columns", {}).get("Column", [])
    out: list[tuple[int, str]] = []
    for i, col in enumerate(cols):  # SA-OK: in-memory QBO report columns — dict access, no DB Session
        if col.get("ColType") != "Money":
            continue
        title = str(col.get("ColTitle", "") or "").strip()
        if "total" in title.lower():
            continue
        out.append((i, title))
    return out


def _summary_value_at(
    report: dict, label_options: list[str], col_idx: int, dq: list[str] | None = None
) -> float | None:
    """Value of a labeled Summary row (e.g. "Net Income") at ``col_idx``, honoring option
    priority. Reads a named total row — not the account-type classification walk.

    ``dq`` (FOS-1 Wave B, AC-1) is an optional data-quality sink: a lookup matching NO
    option records ``label_unresolved:<opt|opt>``; an option matching MULTIPLE Summary
    rows records ``label_ambiguous:<label>`` — and returns the common value when the
    duplicates agree, or an honest None when they conflict (never an arbitrary pick).
    The first option with at least one parseable match OWNS the resolution (ambiguity
    within it does not fall through to a less-specific option). Clean single-match
    lookups behave exactly as before and record nothing."""
    rows = report.get("Rows", {}).get("Row", [])

    def _collect(rows: list, label: str, hits: list[float]) -> None:
        for row in rows:  # SA-OK: in-memory QBO report walk — dict access, not a DB query
            cd = row.get("Summary", {}).get("ColData", [])
            if cd and str(cd[0].get("value", "") or "").strip() == label:
                try:
                    hits.append(_parse_amount(cd[col_idx].get("value", "0")))
                except (IndexError, KeyError, AttributeError):
                    pass  # a matched row without a parseable cell is not a usable hit
            sub = row.get("Rows", {}).get("Row", [])
            if sub:
                _collect(sub, label, hits)

    for label in label_options:  # SA-OK: bounded label-preference list — in-memory, no DB Session
        hits: list[float] = []
        _collect(rows, label, hits)
        if not hits:
            continue
        if len(hits) > 1:
            if dq is not None:
                dq.append(f"label_ambiguous:{label}")
            if any(h != hits[0] for h in hits[1:]):
                return None  # conflicting duplicate rows — honest no-data, never a fabricated pick
        return hits[0]
    if dq is not None:
        dq.append("label_unresolved:" + "|".join(label_options))
    return None


def _entries_by_id_for_month(report: dict, month_key: str) -> dict[str, float]:
    """``{qbo_account_id: amount}`` for one month column, via the canonical parser."""
    out: dict[str, float] = {}
    for e in _parse_pl_single_month_entries(report, month_key=month_key):  # SA-OK: in-memory parsed entries, no DB Session
        aid = e["qbo_account_id"]
        if aid:
            out[str(aid)] = float(e["amount"] or 0.0)
    return out


_DEBT_REPAY_KEYWORDS = ("loan", "note", "debt", "principal", "borrowing", "credit")


def _cf_principal_for_month(
    cf: dict, debt_ids: set[str], col_idx: int
) -> tuple[float | None, str | None]:
    """Per-month principal = abs of the financing-section debt-repayment line (AC-3).

    Walks the Cash-Flow "Financing" section; a repayment is a CASH OUTFLOW (negative amount).
    Scopes to the covenant's mapped debt accounts by account id when the CF lines carry ids;
    otherwise falls back to a label-keyword match within the financing section. Returns
    (principal, None) — 0.0 is a legitimate "no repayment this month". Returns
    (None, "covenant_cf_financing_section_missing") when the financing section itself cannot be
    located (honest no-data, never a fabricated 0, and never a liability-balance diff)."""
    found_financing = False
    total = 0.0

    def _cols(row: dict) -> list:
        return row.get("ColData", []) or row.get("Header", {}).get("ColData", [])

    def _row_amount(row: dict) -> float | None:
        cd = _cols(row)
        if len(cd) <= col_idx:
            return None
        try:
            return _parse_amount(cd[col_idx].get("value", "0"))
        except (IndexError, KeyError, AttributeError):
            return None

    def _row_id_label(row: dict) -> tuple[str, str]:
        cd = _cols(row)
        if not cd:
            return "", ""
        return str(cd[0].get("id", "") or "").strip(), str(cd[0].get("value", "") or "").strip()

    def _accumulate_financing(rows: list) -> None:
        nonlocal total
        for row in rows:  # SA-OK: in-memory QBO CashFlow rows — dict access, no DB Session
            rid, label = _row_id_label(row)
            amt = _row_amount(row)
            is_repay_line = (rid and rid in debt_ids) or (
                not debt_ids and any(k in label.lower() for k in _DEBT_REPAY_KEYWORDS)
            )
            if is_repay_line and amt is not None and amt < 0:
                total += abs(amt)
            sub = row.get("Rows", {}).get("Row", [])
            if sub:
                _accumulate_financing(sub)

    def _walk(rows: list) -> None:
        nonlocal found_financing
        for row in rows:  # SA-OK: in-memory QBO CashFlow walk — dict access, no DB Session
            _rid, label = _row_id_label(row)
            if row.get("type") == "Section" and "financing" in label.lower():
                found_financing = True
                _accumulate_financing(row.get("Rows", {}).get("Row", []))
                continue
            sub = row.get("Rows", {}).get("Row", [])
            if sub:
                _walk(sub)

    _walk(cf.get("Rows", {}).get("Row", []))
    if not found_financing:
        return None, "covenant_cf_financing_section_missing"
    return round(total, 2), None


# ─────────────────────────────────────────────────────────────────────────────
# Month-column join guard + semantic-compat descriptors (FOS-1 Wave B, AC-1/AC-2)
# ─────────────────────────────────────────────────────────────────────────────

_MONTH_NUM = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _month_col_map(
    report: dict, report_tag: str, expected_keys: set[str], dq: list[str]
) -> dict[str, tuple[int, str]]:
    """First-3-of-title month key → (column index, title) for one monthly report, with the
    AC-1 join-mismatch guard: a column whose key matches NO expected P&L month records
    ``month_key_unmatched:<report>:<title>``; two columns collapsing onto the SAME key
    (e.g. "Jan 2020" + "Jan 2021" in a >12-month window) record
    ``month_key_ambiguous:<report>:<key>``. The join itself is unchanged (last-wins,
    exactly what the prior dict-comprehension did) — the guard counts + surfaces, it
    never changes a computed number."""
    out: dict[str, tuple[int, str]] = {}
    for i, title in _money_month_columns(report):  # SA-OK: in-memory parsed columns, no DB Session
        key = title[:3].lower()
        if key not in expected_keys:
            dq.append(f"month_key_unmatched:{report_tag}:{title}")
        if key in out:
            dq.append(f"month_key_ambiguous:{report_tag}:{key}")
        out[key] = (i, title)  # last-wins — byte-identical join behavior to the prior map
    return out


def _period_from_title(title: str) -> Period | None:
    """Parse a QBO month-column title ("Jan 2020") into its full-month Period, or None."""
    parts = str(title or "").strip().split()
    if len(parts) == 2 and parts[1].isdigit():
        month = _MONTH_NUM.get(parts[0][:3].lower())
        if month:
            return Period.month(int(parts[1]), month)
    return None


def _report_basis(report: dict) -> Basis:
    """The accounting basis a report ACTUALLY came back on (the ``Header.ReportBasis`` echo).

    Every request in this module force-pins ``accounting_method="Accrual"``, so ACCRUAL is
    the default when the echo is absent; reading the echo lets the compat wiring catch a
    report QBO returned on a different basis instead of assuming the request was honored."""
    header = report.get("Header", {}) or {}
    raw = str(header.get("ReportBasis", "") or "").strip().lower()
    return Basis.CASH if raw == "cash" else Basis.ACCRUAL


def _covenant_ref(metric_id: str, entity_id: str, period: Period, basis: Basis) -> MetricRef:
    """MetricRef-lite descriptor for one side of a covenant comparison. Scenario is always
    ACTUAL here (the engine reads live QBO actuals); unit comes from the metric registry."""
    return MetricRef(
        metric_id=metric_id,
        entity_id=entity_id,
        period=period,
        basis=basis,
        scenario=Scenario.actual(),
        unit=get_metric(metric_id).unit,
    )


def _pair_compat_codes(
    tag: str, metric_id: str, entity_id: str,
    period_a: Period, basis_a: Basis, period_b: Period, basis_b: Basis,
) -> list[str]:
    """Reason codes (prefixed ``compat:<tag>:``) for one comparison pair — NON-raising
    (``check_comparable``), observability-first: codes land in data_quality, numbers stay."""
    codes = check_comparable(
        _covenant_ref(metric_id, entity_id, period_a, basis_a),
        _covenant_ref(metric_id, entity_id, period_b, basis_b),
    )
    return [f"compat:{tag}:{code}" for code in codes]


# ─────────────────────────────────────────────────────────────────────────────
# The engine
# ─────────────────────────────────────────────────────────────────────────────

def _ratio(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator in (None, 0):
        return None
    return round(numerator / denominator, 4)


def _debt_ids(covenant) -> set[str]:
    raw = getattr(covenant, "debt_service_accounts", None)
    if not raw:
        return set()
    try:
        return {str(x).strip() for x in json.loads(raw) if str(x).strip()}
    except (ValueError, TypeError):
        return set()


# PLAN-EXEMPT: plan 20260630-covenant-redesign fix — the covenant's OWN mapped EBITDA add-backs
# (the builder's interest / D&A / tax pickers, stored on the covenant row) are the source of truth
# for its EBITDA; the company-level config is only a fallback for a covenant that mapped none.
def _series_addbacks(covenant, company_addbacks: list | None) -> list | None:
    """Prefer the covenant's OWN category-tagged EBITDA add-back lines (``ebitda_addbacks``); fall
    back to the company-level config only when the covenant has none. Any non-str/non-list value
    (e.g. a test stub with no such attribute) falls back — so the golden tie-out (a covenant with
    no ``ebitda_addbacks``) keeps using the add-backs passed in."""
    raw = getattr(covenant, "ebitda_addbacks", None)
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            return company_addbacks
        return parsed or company_addbacks
    if isinstance(raw, list) and raw:
        return raw
    return company_addbacks


def compute_monthly_series(
    covenant,
    client,
    company_addbacks: list | None,
    start_date: str,
    end_date: str,
) -> CovenantSeries:
    """Compute the 12-month two-lens grid for ``covenant`` over [start_date, end_date].

    ONE monthly-columned report each: ProfitAndLoss, BalanceSheet, CashFlow. Interest / D&A are
    resolved from ``company_addbacks`` (the company EBITDA config); Total Debt + CF-principal
    scope come from the covenant's ``debt_service_accounts``. Raises RuntimeError only if QBO is
    entirely unreachable; per-component gaps degrade to a month-level ``no_data`` reason.
    """
    metric = str(covenant.metric_type or "").upper()
    # Classify EXPLICITLY — never fall through to a lens. Anything that is not one of the two
    # supported two-lens metrics (DSCR coverage / DEBT_EBITDA leverage) — e.g. MIN_CASH, LEVERAGE,
    # CURRENT_RATIO, CUSTOM — is NOT a monthly two-lens metric; computing it as leverage would
    # fabricate a Debt/EBITDA ratio for it (a plausible-but-wrong lender-facing number). Degrade
    # honestly instead: unavailable + reason, before any QBO call.
    if metric in COVERAGE_METRICS:
        lens = "coverage"
    elif metric in LEVERAGE_METRICS:
        lens = "leverage"
    else:
        lens = "unsupported"
    threshold = float(covenant.threshold) if covenant.threshold is not None else 0.0
    series = CovenantSeries(
        metric_type=metric,
        threshold=threshold,
        breach_direction=str(getattr(covenant, "breach_direction", "BELOW") or "BELOW"),
        lens=lens,
    )
    if lens == "unsupported":
        series.reason = (
            f"The monthly two-lens report supports DSCR and Debt/EBITDA covenants only; "
            f"'{metric or 'this metric'}' is not a monthly two-lens metric."
        )
        return series  # available stays False; no QBO call, no fabricated ratio.
    debt_ids = _debt_ids(covenant)
    # PLAN-EXEMPT: plan 20260630-covenant-redesign fix — EBITDA add-backs are the covenant's OWN
    # mapped interest/D&A/tax (builder), falling back to the company config only when none are mapped.
    addbacks = _series_addbacks(covenant, company_addbacks)

    params = {
        "start_date": start_date,
        "end_date": end_date,
        "summarize_column_by": "Month",
        "accounting_method": "Accrual",
    }
    try:
        pl = client.get_report("ProfitAndLoss", params)
        bs = client.get_report("BalanceSheet", params)
        cf = client.get_report("CashFlow", params)
    except Exception as exc:  # QBO unreachable — surface (chained), never fabricate a number.
        logger.warning("[CovenantCompute] QBO monthly reports failed: %s",
                       redact_to_string(str(exc), max_length=300))
        raise RuntimeError("Covenant data could not be loaded from QuickBooks.") from exc

    pl_cols = _money_month_columns(pl)
    if not pl_cols:
        series.reason = "No monthly P&L columns returned from QuickBooks."
        return series

    # Column-index maps for the BS / CF, keyed by the same first-3-of-title month so the three
    # reports align even if a tenant's column ordering differs — with the AC-1 join guard
    # (_month_col_map): unmatched / ambiguous month keys are counted into series.data_quality.
    pl_keys = [t[:3].lower() for _, t in pl_cols]
    for key in sorted({k for k in pl_keys if pl_keys.count(k) > 1}):
        # Two P&L columns collapsing onto one month key (a >12-month window wraps the same
        # calendar month twice) makes every BS/CF join for that key ambiguous.
        series.data_quality.append(f"month_key_ambiguous:pl:{key}")
    expected_keys = set(pl_keys)
    bs_col_by_key = _month_col_map(bs, "bs", expected_keys, series.data_quality)
    cf_col_by_key = _month_col_map(cf, "cf", expected_keys, series.data_quality)

    # AC-2 compat context: the covenant metric identity from the semantics registry, basis from
    # each report's Header echo (requests force-pin Accrual — the echo says what QBO actually
    # returned), scenario ACTUAL throughout, one entity id shared by every descriptor.
    metric_id = "covenant.dscr" if lens == "coverage" else "covenant.debt_to_ebitda"
    entity_id = str(
        getattr(covenant, "company_id", None) or getattr(covenant, "id", None) or "covenant"
    )
    bases = {"pl": _report_basis(pl), "bs": _report_basis(bs), "cf": _report_basis(cf)}

    ttm = _TtmAccumulator()

    for _pl_idx, title in pl_cols:  # SA-OK: iterating the parsed month columns — in-memory, no DB Session
        month = _compute_one_month(
            title, pl, bs, cf, _pl_idx, bs_col_by_key, cf_col_by_key,
            addbacks, debt_ids, lens, threshold,  # PLAN-EXEMPT: covenant's own add-backs (redesign fix)
            metric_id=metric_id, entity_id=entity_id, bases=bases,
        )
        ttm.add(month)
        series.months.append(month)

    _finalize_ttm(
        series, lens, ttm,
        start_date=start_date, end_date=end_date,
        metric_id=metric_id, entity_id=entity_id, bases=bases,
    )
    return series


@dataclass
class _TtmAccumulator:
    ebitda: float = 0.0
    debt_service: float = 0.0
    has_ebitda: bool = False
    has_debt_service: bool = False
    latest_total_debt: float | None = None
    latest_total_debt_period: str | None = None  # the month title the latest balance came from

    def add(self, m: "CovenantMonth") -> None:
        if m.ebitda is not None:
            self.ebitda += m.ebitda
            self.has_ebitda = True
        if m.debt_service is not None:
            self.debt_service += m.debt_service
            self.has_debt_service = True
        if m.total_debt is not None:
            self.latest_total_debt = m.total_debt
            self.latest_total_debt_period = m.period


def _compute_one_month(
    title, pl, bs, cf, pl_idx, bs_col_by_key, cf_col_by_key,
    company_addbacks, debt_ids, lens, threshold,
    *, metric_id: str, entity_id: str, bases: dict[str, Basis],
) -> CovenantMonth:
    key = title[:3].lower()
    dq: list[str] = []
    net_income = _summary_value_at(pl, ["Net Income", "Net Earnings"], pl_idx, dq=dq)
    pl_by_id = _entries_by_id_for_month(pl, title)
    addback_total, _lines, cat = _resolve_addbacks(company_addbacks, pl_by_id)
    interest = max(0.0, cat["interest"])
    taxes = cat["tax"]
    d_and_a = round(cat["depreciation"] + cat["amortization"], 2)

    ebitda = round(net_income + addback_total, 2) if net_income is not None else None

    total_debt: float | None = None
    bs_entry = bs_col_by_key.get(key)
    if bs_entry is None and (debt_ids or lens == "leverage"):
        # The month exists in the P&L but the (needed) BS has no column for it — the join
        # misses. Counted here; the VALUE outcome stays the existing honest no_data path.
        dq.append(f"month_key_missing:bs:{title}")
    if debt_ids and bs_entry is not None:
        bs_by_id = _entries_by_id_for_month(bs, title)
        total_debt = round(sum(bs_by_id.get(i, 0.0) for i in debt_ids), 2)
    elif lens == "leverage" and bs_entry is not None:
        # PLAN-EXEMPT: plan 20260630-covenant-redesign fix — when a Debt/EBITDA covenant has NO
        # serviced-debt accounts mapped, Total Debt falls back to the Balance Sheet
        # "Total Liabilities" row, the SAME source the single-period /status Debt/EBITDA verdict
        # uses (coa_formula._compute_debt_ebitda). So the monthly grid AGREES with the header value
        # instead of degrading to "could not be computed". Mapped accounts still take precedence;
        # this is the honest no-mapping default, never a fabricated 0.
        tl = _summary_value_at(bs, ["Total Liabilities"], bs_entry[0], dq=dq)
        total_debt = round(tl, 2) if tl is not None else None

    principal: float | None = None
    principal_reason: str | None = None
    cf_entry = cf_col_by_key.get(key)
    if cf_entry is None:
        dq.append(f"month_key_missing:cf:{title}")
    else:
        principal, principal_reason = _cf_principal_for_month(cf, debt_ids, cf_entry[0])

    debt_service = round(interest + principal, 2) if principal is not None else None

    run_rate = _month_run_rate(lens, ebitda, debt_service, total_debt)
    run_rate_not_meaningful = (
        "covenant_ebitda_nonpositive"
        if lens == "leverage" and ebitda is not None and total_debt is not None and ebitda <= 0
        else None
    )
    overage = None
    if lens == "coverage" and ebitda is not None and debt_service is not None:
        overage = round(ebitda - debt_service * threshold, 2)

    # AC-2 (FOS-1 Wave B): semantic-compat check on the month's cross-report comparison pair.
    # Observability-first — reason codes land in data_quality, the numbers above are untouched.
    _month_compat(
        dq, lens=lens, metric_id=metric_id, entity_id=entity_id, bases=bases,
        pl_title=title,
        bs_title=bs_entry[1] if bs_entry is not None else None,
        cf_title=cf_entry[1] if cf_entry is not None else None,
        ebitda=ebitda, debt_service=debt_service, total_debt=total_debt,
    )

    return CovenantMonth(
        period=title, net_income=net_income, interest=round(interest, 2), taxes=round(taxes, 2),
        d_and_a=d_and_a, ebitda=ebitda, principal=principal, debt_service=debt_service,
        total_debt=total_debt, run_rate_ratio=run_rate, overage=overage,
        no_data=_month_no_data(ebitda, debt_service, total_debt, lens, principal_reason),
        data_quality=dq,
        run_rate_not_meaningful=run_rate_not_meaningful,
    )


def _month_compat(
    dq: list[str], *, lens: str, metric_id: str, entity_id: str, bases: dict[str, Basis],
    pl_title: str, bs_title: str | None, cf_title: str | None,
    ebitda: float | None, debt_service: float | None, total_debt: float | None,
) -> None:
    """AC-2: run the Wave-A comparability checker over the month's ACTUAL comparison pair —
    coverage compares PL-sourced EBITDA against CF-sourced debt service (``pl_vs_cf``);
    leverage compares BS-sourced Total Debt against PL-sourced EBITDA (``bs_vs_pl``). Both
    sides are described under the covenant's registry metric identity (the ratio IS the metric
    under test); each side's period comes from ITS OWN report column title and its basis from
    that report's Header echo. A month whose comparison was never made (a side is None) is
    skipped — its ``no_data`` already says why. Never changes a computed number."""
    pl_period = _period_from_title(pl_title)
    if pl_period is None:
        dq.append(f"month_title_unparsed:pl:{pl_title}")
        return
    if lens == "coverage":
        if ebitda is None or debt_service is None or cf_title is None:
            return
        cf_period = _period_from_title(cf_title)
        if cf_period is None:
            dq.append(f"month_title_unparsed:cf:{cf_title}")
            return
        dq.extend(_pair_compat_codes(
            "pl_vs_cf", metric_id, entity_id,
            pl_period, bases["pl"], cf_period, bases["cf"],
        ))
        return
    # leverage
    if ebitda is None or total_debt is None or bs_title is None:
        return
    bs_period = _period_from_title(bs_title)
    if bs_period is None:
        dq.append(f"month_title_unparsed:bs:{bs_title}")
        return
    dq.extend(_pair_compat_codes(
        "bs_vs_pl", metric_id, entity_id,
        bs_period, bases["bs"], pl_period, bases["pl"],
    ))


def _month_run_rate(lens: str, ebitda, debt_service, total_debt) -> float | None:
    if lens == "coverage":
        return _ratio(ebitda, debt_service)                              # EBITDA / debt service
    if ebitda is None or ebitda <= 0:
        return None  # EBITDA <= 0: not meaningful (flagged on the month), never a negative multiple
    return _ratio(total_debt, ebitda * MONTHS_PER_YEAR)                  # debt / (EBITDA × 12)


def _month_no_data(ebitda, debt_service, total_debt, lens, principal_reason) -> str | None:
    if ebitda is None:
        return "covenant_ebitda_unavailable"
    if lens == "coverage" and debt_service is None:
        return principal_reason or "covenant_debt_service_unavailable"
    if lens == "leverage" and total_debt is None:
        return "covenant_total_debt_unavailable"
    return None


def _ttm_window_period(start_date: str, end_date: str) -> Period | None:
    """The TTM-grain Period for the engine's [start_date, end_date] window, or None when the
    ISO date strings do not parse (recorded by the caller — never silently assumed)."""
    try:
        return Period(
            date.fromisoformat(str(start_date)), date.fromisoformat(str(end_date)),
            PeriodGrain.TTM,
        )
    except (ValueError, TypeError):
        return None


def _finalize_ttm(
    series: CovenantSeries, lens: str, ttm: _TtmAccumulator,
    *, start_date: str, end_date: str, metric_id: str, entity_id: str, bases: dict[str, Basis],
) -> None:
    ttm_period = _ttm_window_period(start_date, end_date)
    if lens == "coverage":
        if ttm.has_ebitda and ttm.has_debt_service and ttm.debt_service:
            series.ttm_ratio = round(ttm.ebitda / ttm.debt_service, 4)
            series.ttm_overage = round(ttm.ebitda - ttm.debt_service * series.threshold, 2)
            # AC-2: TTM EBITDA (P&L) vs TTM debt service (CF) over the same TTM window.
            if ttm_period is None:
                series.data_quality.append(f"ttm_window_unparsed:{start_date}..{end_date}")
            else:
                series.data_quality.extend(_pair_compat_codes(
                    "ttm:pl_vs_cf", metric_id, entity_id,
                    ttm_period, bases["pl"], ttm_period, bases["cf"],
                ))
    elif ttm.has_ebitda and ttm.latest_total_debt is not None and ttm.ebitda <= 0:
        # leverage with trailing-12 EBITDA <= 0: the ratio is unbounded, not a (negative) multiple.
        series.ttm_not_meaningful = "covenant_ebitda_nonpositive"
    else:  # leverage — latest balance / trailing-12 EBITDA
        if ttm.has_ebitda and ttm.latest_total_debt is not None:
            series.ttm_ratio = round(ttm.latest_total_debt / ttm.ebitda, 4)
            # AC-2: the latest Total Debt balance (BS, its own month) vs trailing-12 EBITDA
            # (P&L, the TTM window). Cross-grain month-vs-TTM is a deliberate covenant move
            # (as-tested leverage) — the checker flags basis/entity/unit drift, not the grain.
            if ttm_period is None:
                series.data_quality.append(f"ttm_window_unparsed:{start_date}..{end_date}")
            else:
                debt_period = (
                    _period_from_title(ttm.latest_total_debt_period or "") or ttm_period
                )
                series.data_quality.extend(_pair_compat_codes(
                    "ttm:bs_vs_pl", metric_id, entity_id,
                    debt_period, bases["bs"], ttm_period, bases["pl"],
                ))
    series.available = series.ttm_ratio is not None or series.ttm_not_meaningful is not None
    if series.ttm_not_meaningful is not None:
        series.reason = NOT_MEANINGFUL_REASONS[series.ttm_not_meaningful]
    elif not series.available:
        series.reason = (
            "Covenant metric could not be computed from QuickBooks — "
            "check the EBITDA add-back mapping and the serviced debt accounts."
        )

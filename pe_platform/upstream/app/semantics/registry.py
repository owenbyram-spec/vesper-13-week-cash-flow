"""Canonical metric registry — the single vocabulary Peregrine computes today.

Converged (plan 20260712-semantic-layer-convergence) from two former layers: the
FOS ``app/semantics`` registry (terse pl.*/bs.* seed + ``resolve_alias`` slug
machinery + STOCK/FLOW kind grading the compat layer needs) and the former
``app/services/semantics`` catalog (the richer ``income_statement.*`` / balance_sheet
/ ar / ap / lbo vocabulary the W2 evidence lane emits + binds, with statement /
expected_sign / basis_sensitive metadata). ONE id per concept: the runtime-emitted
descriptive ids are canonical; the former terse ids + MetricSnapshot slugs live on
as aliases so ``resolve_alias`` keeps resolving them.

The registry answers what every consumer needs:

- WHAT KIND a metric is — STOCK (a point-in-time balance) vs FLOW (period activity).
  ``kind`` IS the value-kind; ``value_kind`` is a spelling alias for the W2 lane.
- WHICH canonical metric a legacy slug/alias refers to — via ``resolve_alias``
  (``ar_total`` / ``cash_blended`` / ``cash:<id>`` / ``DSCR`` …). Aliases ending in
  ``:`` are PREFIX aliases (``cash:`` matches ``cash:123``).
- The registry's binding metadata (``display_name`` / ``label``, ``aliases``,
  ``unit``) drives W2 prose→metric binding; ``basis_sensitive`` + ``kind`` drive
  comparability.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from .refs import ExpectedSign, Statement, Unit

__all__ = [
    "METRIC_REGISTRY",
    "MetricDef",
    "MetricKind",
    "get_metric",
    "resolve_alias",
    "validate_catalog",
]

_METRIC_ID_RE = re.compile(r"^[a-z_]+\.[a-z0-9_]+$")


class MetricKind(StrEnum):
    """STOCK = point-in-time balance; FLOW = activity over a period. This IS the
    value-kind the W2 evidence lane calls ``value_kind``."""

    STOCK = "stock"
    FLOW = "flow"


@dataclass(frozen=True, slots=True)
class MetricDef:
    """The registry row for one canonical metric.

    ``owner_module`` is the importable app module that computes the metric today
    (registry-integrity tests assert it resolves via ``importlib.util.find_spec``);
    ``source_modules`` is the richer multi-module attribution. ``aliases`` are legacy
    slugs / key names that map to this metric; an alias ending in ``:`` is a prefix
    pattern. ``value_kind`` / ``label`` are spelling aliases (properties) for the W2
    evidence + comparability consumers.
    """

    metric_id: str
    display_name: str
    kind: MetricKind
    unit: Unit
    owner_module: str
    source: str
    version: int = 1
    aliases: tuple[str, ...] = ()
    statement: Statement = Statement.UNSPECIFIED
    expected_sign: ExpectedSign = ExpectedSign.EITHER
    basis_sensitive: bool = True
    source_modules: tuple[str, ...] = ()

    @property
    def value_kind(self) -> MetricKind:
        """Spelling alias — STOCK/FLOW IS the value-kind (W2 lane vocabulary)."""
        return self.kind

    @property
    def label(self) -> str:
        """Spelling alias for ``display_name`` (W2 binding vocabulary)."""
        return self.display_name


_IS = Statement.INCOME_STATEMENT
_BS = Statement.BALANCE_SHEET
_CF = Statement.CASH_FLOW
_CFF = Statement.CASHFLOW_FORECAST
_POS = ExpectedSign.POSITIVE
_EITHER = ExpectedSign.EITHER


def _m(
    metric_id: str,
    display_name: str,
    kind: MetricKind,
    unit: Unit,
    owner_module: str,
    source: str,
    *,
    statement: Statement = Statement.UNSPECIFIED,
    expected_sign: ExpectedSign = _EITHER,
    basis_sensitive: bool = True,
    aliases: tuple[str, ...] = (),
    source_modules: tuple[str, ...] = (),
) -> MetricDef:
    return MetricDef(
        metric_id=metric_id, display_name=display_name, kind=kind, unit=unit,
        owner_module=owner_module, source=source, aliases=aliases,
        statement=statement, expected_sign=expected_sign,
        basis_sensitive=basis_sensitive, source_modules=source_modules,
    )


_SEED: tuple[MetricDef, ...] = (
    # ── Income statement (P&L walkers + COA formula engine) ─────────────────────
    _m("income_statement.total_revenue", "Total Revenue", MetricKind.FLOW, Unit.CURRENCY,
       "app.services.coa_formula", "P&L Income section (compute_period_pl_kpis / TTM)",
       statement=_IS, expected_sign=_POS,
       aliases=("total income", "revenue", "sales", "total_revenue", "period_revenue", "ttm_revenue"),
       source_modules=("app/services/coa_formula.py", "app/routers/reports.py")),
    _m("income_statement.total_expenses", "Total Expenses", MetricKind.FLOW, Unit.CURRENCY,
       "app.services.coa_formula", "P&L Expenses section (reported positive)",
       statement=_IS, expected_sign=_POS, aliases=("expenses", "total_expenses"),
       source_modules=("app/services/coa_formula.py",)),
    _m("income_statement.gross_profit", "Gross Profit", MetricKind.FLOW, Unit.CURRENCY,
       "app.services.coa_formula", "Revenue less COGS for the period",
       statement=_IS, aliases=("gross_profit", "ttm_gross_profit", "period_gross_profit"),
       source_modules=("app/services/coa_formula.py",)),
    _m("income_statement.net_income", "Net Income", MetricKind.FLOW, Unit.CURRENCY,
       "app.services.coa_formula", "Bottom-line profit/loss for the period",
       statement=_IS, aliases=("net_income", "period_net_income"),
       source_modules=("app/services/coa_formula.py",)),
    _m("income_statement.ebitda", "EBITDA", MetricKind.FLOW, Unit.CURRENCY,
       "app.services.coa_formula", "Net income + mapped add-backs (explicit MAPPING, never keyword inference)",
       statement=_IS, aliases=("ebitda", "ttm_ebitda", "period_ebitda"),
       source_modules=("app/services/coa_formula.py",)),
    _m("income_statement.gross_margin_pct", "Gross Margin %", MetricKind.FLOW, Unit.PERCENT,
       "app.services.coa_formula", "Gross profit as a percent of revenue", statement=_IS,
       source_modules=("app/services/coa_formula.py",)),
    _m("income_statement.net_margin_pct", "Net Margin %", MetricKind.FLOW, Unit.PERCENT,
       "app.services.coa_formula", "Net income as a percent of revenue", statement=_IS,
       source_modules=("app/services/coa_formula.py",)),
    _m("income_statement.ebitda_margin_pct", "EBITDA Margin %", MetricKind.FLOW, Unit.PERCENT,
       "app.routers.intelligence", "EBITDA as a percent of revenue (the ebitda-trend surface plots THIS)",
       statement=_IS, source_modules=("app/services/coa_formula.py", "app/routers/intelligence.py")),
    _m("pl.opex", "Operating expenses", MetricKind.FLOW, Unit.CURRENCY,
       "app.services.coa_formula", "QBO ProfitAndLoss Expenses section (covenant/dashboard lens)",
       aliases=("opex", "operating_expenses", "period_operating_expenses"),
       source_modules=("app/services/coa_formula.py",)),
    # ── Balance sheet ───────────────────────────────────────────────────────────
    _m("balance_sheet.total_assets", "Total Assets", MetricKind.STOCK, Unit.CURRENCY,
       "app.routers.reports", "Total assets as of the period end", statement=_BS,
       expected_sign=_POS, aliases=("total_assets",), source_modules=("app/routers/reports.py",)),
    _m("balance_sheet.total_liabilities", "Total Liabilities", MetricKind.STOCK, Unit.CURRENCY,
       "app.routers.reports", "Total liabilities as of the period end (covenant leverage fallback)",
       statement=_BS, expected_sign=_POS, aliases=("total_liabilities",),
       source_modules=("app/routers/reports.py", "app/services/covenant_compute.py")),
    _m("balance_sheet.total_equity", "Total Equity", MetricKind.STOCK, Unit.CURRENCY,
       "app.routers.reports", "Total equity as of the period end", statement=_BS,
       aliases=("total_equity",), source_modules=("app/routers/reports.py",)),
    _m("balance_sheet.cash_balance", "Cash Balance", MetricKind.STOCK, Unit.CURRENCY,
       "app.services.account_balances", "Cash and equivalents as of a date (a bank fact, basis-independent)",
       statement=_BS, expected_sign=_POS, basis_sensitive=False,
       aliases=("cash_blended", "cash:"), source_modules=("app/services/account_balances.py",)),
    _m("balance_sheet.total_debt", "Total Debt", MetricKind.STOCK, Unit.CURRENCY,
       "app.services.covenant_compute", "Interest-bearing debt outstanding as of a date",
       statement=_BS, expected_sign=_POS, basis_sensitive=False, aliases=("total_debt",),
       source_modules=("app/services/covenant_compute.py", "app/lbo/debt_schedule.py")),
    # ── Cash flow (CFF statement walkers) ───────────────────────────────────────
    _m("cash_flow.operating", "Operating Cash Flow", MetricKind.FLOW, Unit.CURRENCY,
       "app.cashflow", "Net cash from operating activities (NOT the cash balance)",
       statement=_CF, basis_sensitive=False, source_modules=("app/cashflow/",)),
    _m("cash_flow.investing", "Investing Cash Flow", MetricKind.FLOW, Unit.CURRENCY,
       "app.cashflow", "Net cash from investing activities", statement=_CF,
       basis_sensitive=False, source_modules=("app/cashflow/",)),
    _m("cash_flow.financing", "Financing Cash Flow", MetricKind.FLOW, Unit.CURRENCY,
       "app.cashflow", "Net cash from financing activities (loan proceeds live HERE)",
       statement=_CF, basis_sensitive=False, source_modules=("app/cashflow/",)),
    _m("cash_flow.net_change", "Net Change in Cash", MetricKind.FLOW, Unit.CURRENCY,
       "app.cashflow", "Total change in cash for the period", statement=_CF,
       basis_sensitive=False, source_modules=("app/cashflow/",)),
    # ── 13-week forecast lane ───────────────────────────────────────────────────
    _m("cashflow_forecast.week_ending_cash", "Week-Ending Cash", MetricKind.STOCK, Unit.CURRENCY,
       "app.routers.cashflow_forecast", "Projected cash balance at a forecast week end",
       statement=_CFF, basis_sensitive=False,
       aliases=("ending cash", "projected cash balance"),
       source_modules=("app/cashflow/", "app/routers/cashflow_forecast.py")),
    _m("cashflow_forecast.min_cash_week_balance", "Minimum-Cash Week Balance", MetricKind.STOCK,
       Unit.CURRENCY, "app.cashflow", "The lowest projected week-ending cash balance in the horizon",
       statement=_CFF, basis_sensitive=False,
       aliases=("minimum projected cash", "lowest projected cash", "min cash week", "min_cash_balance"),
       source_modules=("app/cashflow/",)),
    _m("cashflow.weekly_net_flow", "Weekly cash forecast net flow", MetricKind.FLOW, Unit.CURRENCY,
       "app.routers.cashflow_forecast", "13-week grid: cash_in − cash_out per week (CASH basis)",
       basis_sensitive=False, aliases=("weekly_net_flow",),
       source_modules=("app/routers/cashflow_forecast.py",)),
    # ── AR / AP ─────────────────────────────────────────────────────────────────
    _m("ar.total_outstanding", "AR Outstanding", MetricKind.STOCK, Unit.CURRENCY,
       "app.services.ar_ap_summary", "Total accounts receivable open as of a date",
       statement=Statement.AR, expected_sign=_POS,
       aliases=("accounts receivable", "receivables", "ar_total", "total_ar"),
       source_modules=("app/services/ar_ap_summary.py",)),
    _m("ar.overdue_total", "AR Overdue", MetricKind.STOCK, Unit.CURRENCY,
       "app.services.ar_ap_summary", "Receivables past due as of a date (aging buckets sum)",
       statement=Statement.AR, expected_sign=_POS,
       aliases=("overdue receivables", "past due receivables"),
       source_modules=("app/services/ar_ap_summary.py",)),
    _m("ar.dso_days", "Days Sales Outstanding", MetricKind.STOCK, Unit.DAYS,
       "app.analysis.payment_behavior", "Average days to collect receivables",
       statement=Statement.AR, expected_sign=_POS, aliases=("dso",),
       source_modules=("app/analysis/payment_behavior.py",)),
    _m("ap.total_outstanding", "AP Outstanding", MetricKind.STOCK, Unit.CURRENCY,
       "app.services.ar_ap_summary", "Total accounts payable open as of a date",
       statement=Statement.AP, expected_sign=_POS,
       aliases=("accounts payable", "payables", "ap_total", "total_ap"),
       source_modules=("app/services/ar_ap_summary.py",)),
    _m("ap.dpo_days", "Days Payables Outstanding", MetricKind.STOCK, Unit.DAYS,
       "app.analysis.payment_behavior", "Average days taken to pay suppliers",
       statement=Statement.AP, expected_sign=_POS, aliases=("dpo",),
       source_modules=("app/analysis/payment_behavior.py",)),
    _m("ar.aging_bucket", "AR aging bucket balance", MetricKind.STOCK, Unit.CURRENCY,
       "app.analysis.ar_aging", "QBO AgedReceivables bucket columns (rides the 'bucket' dimension)",
       aliases=("ar_aging_bucket",), source_modules=("app/analysis/ar_aging.py",)),
    _m("ap.aging_bucket", "AP aging bucket balance", MetricKind.STOCK, Unit.CURRENCY,
       "app.analysis.ap_aging", "QBO AgedPayables bucket columns (rides the 'bucket' dimension)",
       aliases=("ap_aging_bucket",), source_modules=("app/analysis/ap_aging.py",)),
    # ── Covenant metrics (FOS kind grading retained — the deployed covenant engine
    #    kind-grades against these) ─────────────────────────────────────────────
    _m("covenant.dscr", "DSCR", MetricKind.FLOW, Unit.RATIO,
       "app.services.covenant_compute", "EBITDA / (interest + principal) over the period",
       statement=Statement.COVENANT, expected_sign=_POS, aliases=("dscr", "DSCR"),
       source_modules=("app/services/covenant_compute.py",)),
    _m("covenant.debt_to_ebitda", "Debt / EBITDA", MetricKind.STOCK, Unit.RATIO,
       "app.services.covenant_compute", "Latest total-debt balance / trailing-12 EBITDA (as-tested)",
       statement=Statement.COVENANT, expected_sign=_POS, aliases=("debt_to_ebitda", "DEBT_EBITDA"),
       source_modules=("app/services/covenant_compute.py",)),
    _m("covenant.leverage", "Leverage (covenant builtin)", MetricKind.STOCK, Unit.RATIO,
       "app.services.coa_formula", "compute_builtin metric_type LEVERAGE",
       statement=Statement.COVENANT, expected_sign=_POS, aliases=("leverage", "LEVERAGE"),
       source_modules=("app/services/coa_formula.py",)),
    _m("covenant.current_ratio", "Current Ratio", MetricKind.STOCK, Unit.RATIO,
       "app.services.covenant_compute", "Current assets / current liabilities as of a date",
       statement=Statement.COVENANT, expected_sign=_POS, aliases=("current_ratio",),
       source_modules=("app/services/covenant_compute.py",)),
    _m("covenant.min_cash", "Minimum Cash", MetricKind.STOCK, Unit.CURRENCY,
       "app.services.coa_formula", "Cash balance vs a minimum-liquidity covenant threshold",
       statement=Statement.COVENANT, expected_sign=_POS, basis_sensitive=False,
       aliases=("min_cash", "MIN_CASH"), source_modules=("app/services/covenant_compute.py",)),
    # ── Budget lane ─────────────────────────────────────────────────────────────
    _m("budget.line_amount", "Budget line amount", MetricKind.FLOW, Unit.CURRENCY,
       "app.budget", "Budget line monthly_amounts rolled up (a BUDGET-scenario flow)",
       statement=Statement.BUDGET, aliases=("budget_line_amount", "monthly_amount"),
       source_modules=("app/budget/rollups.py",)),
    _m("budget.variance_amount", "Budget Variance ($)", MetricKind.FLOW, Unit.CURRENCY,
       "app.budget", "Actual minus budget for a line/period (an EXPLICIT cross-scenario compare)",
       statement=Statement.BUDGET, source_modules=("app/budget/", "app/services/budget_drivers.py")),
    _m("budget.variance_pct", "Budget Variance (%)", MetricKind.FLOW, Unit.PERCENT,
       "app.budget", "Variance as a percent of budget for a line/period",
       statement=Statement.BUDGET, source_modules=("app/budget/",)),
    # ── LBO returns ─────────────────────────────────────────────────────────────
    _m("lbo.moic", "MOIC", MetricKind.FLOW, Unit.RATIO,
       "app.lbo", "Multiple on invested capital at exit (deal-level, scenario=forecast)",
       statement=Statement.LBO, expected_sign=_POS, basis_sensitive=False,
       source_modules=("app/lbo/",)),
    _m("lbo.irr", "IRR", MetricKind.FLOW, Unit.PERCENT,
       "app.lbo", "Internal rate of return at exit (deal-level, scenario=forecast)",
       statement=Statement.LBO, basis_sensitive=False, source_modules=("app/lbo/",)),
    # ── Custom KPIs (named confidence gap — per-KPI kind/unit is user-defined) ───
    _m("custom.kpi", "Custom KPI value", MetricKind.STOCK, Unit.CURRENCY,
       "app.routers.custom_kpis",
       "User-defined formula over current balances + built-in KPIs. DEFAULT reading only; "
       "true kind/unit are per-KPI. The KPI id rides the 'kpi_id' dimension (slug custom:<id>)",
       aliases=("custom:",), source_modules=("app/routers/custom_kpis.py",)),
)


def _build_registry(defs: tuple[MetricDef, ...]) -> dict[str, MetricDef]:
    """Index defs by metric_id, refusing duplicate ids or ambiguous aliases."""
    registry: dict[str, MetricDef] = {}
    seen_aliases: dict[str, str] = {}
    for d in defs:
        if d.metric_id in registry:
            raise ValueError(f"duplicate metric_id in registry seed: {d.metric_id!r}")
        registry[d.metric_id] = d
        for alias in d.aliases:
            if alias in seen_aliases:
                raise ValueError(
                    f"alias {alias!r} is claimed by both {seen_aliases[alias]!r} "
                    f"and {d.metric_id!r}"
                )
            seen_aliases[alias] = d.metric_id
    for alias, owner in seen_aliases.items():
        if alias in registry and registry[alias].metric_id != owner:
            raise ValueError(f"alias {alias!r} shadows canonical metric_id {alias!r}")
    return registry


METRIC_REGISTRY: dict[str, MetricDef] = _build_registry(_SEED)


def get_metric(metric_id: str, *, registry: dict[str, MetricDef] | None = None) -> MetricDef | None:
    """The MetricDef for a canonical ``metric_id``, or None when unknown.

    Returns None (never raises) — the W2 evidence lane + handlers treat an unknown
    id as "not a catalog metric" and branch on None; covenant/budget only ever pass
    ids that are in the registry.
    """
    reg = METRIC_REGISTRY if registry is None else registry
    return reg.get(metric_id)


def validate_catalog() -> list[str]:
    """Structural integrity errors in the merged registry (empty ⇒ valid). Run by
    tests AND importable by the W7 release-gate script. Duplicate-id / ambiguous-alias
    are already refused fail-closed at ``_build_registry`` import; this adds the
    per-row shape checks. The prefix==statement rule applies only to CLASSIFIED rows
    (statement != UNSPECIFIED) — the merged registry deliberately carries a few
    unclassified terse-id rows (pl.opex / aging buckets / custom.kpi)."""
    errors: list[str] = []
    for d in METRIC_REGISTRY.values():
        if not _METRIC_ID_RE.match(d.metric_id):
            errors.append(f"{d.metric_id}: id must match {_METRIC_ID_RE.pattern}")
        if d.statement is not Statement.UNSPECIFIED and d.metric_id.split(".", 1)[0] != d.statement.value:
            errors.append(f"{d.metric_id}: id prefix must equal statement '{d.statement.value}'")
        if not d.display_name.strip() or not d.source.strip():
            errors.append(f"{d.metric_id}: display_name/source must be non-empty")
        if d.version < 1:
            errors.append(f"{d.metric_id}: version must be >= 1")
    return errors


def resolve_alias(slug: str, *, registry: dict[str, MetricDef] | None = None) -> MetricDef:
    """Resolve a slug — canonical id, exact alias, or prefix alias — to its MetricDef.

    Resolution order: canonical metric_id, exact alias, then the LONGEST matching
    prefix alias (aliases ending in ``:``, e.g. ``cash:`` matches ``cash:123``).
    Raises KeyError when nothing matches (refuse over fabricate — an unknown slug
    must never silently resolve to a default metric).
    """
    reg = METRIC_REGISTRY if registry is None else registry
    if slug in reg:
        return reg[slug]
    prefix_hits: list[tuple[int, MetricDef]] = []
    for d in reg.values():
        for alias in d.aliases:
            if alias == slug:
                return d
            if alias.endswith(":") and slug.startswith(alias) and len(slug) > len(alias):
                prefix_hits.append((len(alias), d))
    if prefix_hits:
        prefix_hits.sort(key=lambda pair: pair[0], reverse=True)
        return prefix_hits[0][1]
    raise KeyError(f"unknown metric slug {slug!r} — no canonical id, alias, or prefix matches")

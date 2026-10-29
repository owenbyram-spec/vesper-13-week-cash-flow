from __future__ import annotations

from typing import Any, Dict

from ..models import (
    LboCapexDriver,
    LboCompany,
    LboDebtTranche,
    LboDriver,
    LboEmployee,
    LboPayrollDriver,
    LboRevenueStream,
    LboScenario,
)


DEAL_KEYS = [
    "entry_ebitda_multiple",
    "entry_enterprise_value",
    "exit_ebitda_multiple",
    "transaction_fees_pct",
    "goodwill_amount",
    "equity_carry_pct",
    "exit_year",
    # Phase 2A (plan 20260706 AC-2A.1) deal keys threaded to the exit engine:
    # the stated exit-EBITDA basis and the management-rollover second equity class.
    "exit_ebitda_basis",
    "management_rollover_amount",
    # Chunk C1 (plan 20260716-five-domain-remediation AC-C1c): the cash-sweep /
    # min-cash / distribution deal terms round-trip through the deal-config
    # endpoints (GET/PATCH /deal-drivers). Defaults live on LboAssumptions
    # (app/lbo/assumptions.py) — flat 100% sweep, no floor, no distributions —
    # so exposing them changes no number until a user edits one.
    "sweep_pct",
    "sweep_pct_by_year",
    "min_cash_floor",
    "distribution_pct_of_fcf",
    # AC-C4 (plan 20260716-five-domain-remediation) — market-standard mechanics
    # deal terms, both DEFAULT-OFF so exposing them changes no stored number:
    # the IRC §197 tax-goodwill election (cash-tax linkage, forecast_engine) and
    # the optional preferred-return carry hurdle (exit_waterfall).
    "section_197_election",
    "preferred_return_pct",
    # Deal basis (sources_uses.resolve_deal_basis): how the target's pre-deal cash
    # is priced — cash-free/debt-free (default; the seller keeps the cash and the
    # sponsor funds minimum_operating_cash) or an equity purchase (EV + cash − debt).
    "deal_basis",
    "minimum_operating_cash",
]


def _lbo_company_to_dict(company: LboCompany) -> Dict:
    return {
        "id": company.id,
        "company_id": company.company_id,
        "company_name": company.company_name,
        "close_date": str(company.close_date) if company.close_date else None,
        "currency": company.currency,
        "fiscal_year_end": company.fiscal_year_end,
        "model_start_year": company.model_start_year,
        "model_end_year": company.model_end_year,
        "exit_year": company.exit_year,
        "default_tax_rate": float(company.default_tax_rate) if company.default_tax_rate else 0.21,
        "granularity": company.granularity,
    }


def _scenario_to_dict(scenario: LboScenario) -> Dict:
    return {
        "id": scenario.id,
        "company_id": scenario.company_id,
        "lbo_company_id": scenario.lbo_company_id,
        "name": scenario.name,
        "is_active": scenario.is_active,
        "cash_up_front": float(scenario.cash_up_front or 0),
        "deferred_payment": float(scenario.deferred_payment or 0),
        "senior_debt_amount": float(scenario.senior_debt_amount or 0),
        "notes": scenario.notes,
    }


def _payroll_to_dict(payroll: LboPayrollDriver) -> Dict:
    return {
        "id": payroll.id,
        "department": payroll.department,
        "year": payroll.year,
        "ftes": float(payroll.ftes or 0),
        "avg_salary": float(payroll.avg_salary or 0),
        "payroll_tax_pct": float(payroll.payroll_tax_pct or 0),
        "benefits_pct": float(payroll.benefits_pct or 0),
        "merit_increase_pct": float(payroll.merit_increase_pct or 0),
        "salary_gl_account_id": payroll.salary_gl_account_id,
        "salary_gl_account_name": payroll.salary_gl_account_name,
        "tax_gl_account_id": payroll.tax_gl_account_id,
        "tax_gl_account_name": payroll.tax_gl_account_name,
        "benefits_gl_account_id": payroll.benefits_gl_account_id,
        "benefits_gl_account_name": payroll.benefits_gl_account_name,
        "fte_driver_type": getattr(payroll, "fte_driver_type", "yoy_growth") or "yoy_growth",
        "fte_driver_value": float(getattr(payroll, "fte_driver_value", 0) or 0),
        "fte_driver_rev_per_fte": float(getattr(payroll, "fte_driver_rev_per_fte", 0) or 0),
        "sort_order": getattr(payroll, "sort_order", 0),
    }


def _employee_to_dict(employee: LboEmployee) -> Dict:
    return {
        "id": employee.id,
        "employee_name": employee.employee_name,
        "job_title": employee.job_title,
        "department": employee.department,
        "start_date": employee.start_date,
        "annual_salary": float(employee.annual_salary or 0),
        "salary_gl_account_id": employee.salary_gl_account_id,
        "salary_gl_account_name": employee.salary_gl_account_name,
        "sort_order": employee.sort_order,
    }


def _capex_to_dict(capex: LboCapexDriver) -> Dict:
    return {
        "id": capex.id,
        "category": capex.category,
        "gl_account_id": capex.gl_account_id,
        "gl_account_name": capex.gl_account_name,
        "asset_type": capex.asset_type or "tangible",
        "beginning_gross": float(capex.beginning_gross or 0),
        "beginning_accum_depr": float(capex.beginning_accum_depr or 0),
        "useful_life_years": capex.useful_life_years,
        "capex_pct_revenue": float(capex.capex_pct_revenue or 0),
        "salvage_value_pct": float(capex.salvage_value_pct or 0),
        "hist_avg_spend": float(capex.hist_avg_spend or 0),
        "hist_avg_pct_rev": float(capex.hist_avg_pct_rev or 0),
        "sort_order": getattr(capex, "sort_order", 0),
    }


def _debt_to_dict(tranche: LboDebtTranche) -> Dict:
    return {
        "id": tranche.id,
        "tranche_name": tranche.tranche_name,
        "gl_account_id": tranche.gl_account_id,
        "gl_account_name": tranche.gl_account_name,
        "start_date": tranche.start_date,
        "maturity_date": tranche.maturity_date,
        "current_balance": float(tranche.current_balance or 0),
        "target_balance": float(tranche.target_balance or 0),
        # PLAN-EXEMPT: CI-red hotfix (Steven-directed) — `or` treated a REAL stored 0
        # (zero-spread / zero-rate tranche) as falsy and fabricated the prefill default
        # (0 rendered as 200 bps). Only a genuinely-absent value gets the default.
        "sofr_rate": float(tranche.sofr_rate) if tranche.sofr_rate is not None else 0.043,
        "spread_bps": int(tranche.spread_bps) if tranche.spread_bps is not None else 200,
        "funded_amount": float(tranche.funded_amount or 0),
        "interest_rate": float(tranche.interest_rate or 0),
        "rate_type": tranche.rate_type,
        "base_rate": float(tranche.base_rate or 0),
        "spread": float(tranche.spread or 0),
        "amort_years": tranche.amort_years,
        "bullet_pct": float(tranche.bullet_pct or 0),
        "pik_flag": tranche.pik_flag,
        "fees_pct": float(tranche.fees_pct or 0),
        "sort_order": tranche.sort_order,
        # Sweep / revolver mechanics (chunk C1 AC-C1c) — round-trip through
        # GET/PUT /scenarios/{id}/debt so the deal panel can edit them.
        "cash_sweep_flag": bool(tranche.cash_sweep_flag),
        "revolver_flag": bool(tranche.revolver_flag),
        "commitment": float(tranche.commitment) if tranche.commitment is not None else None,
        "seniority_rank": tranche.seniority_rank,
    }


def _revenue_stream_to_dict(stream: LboRevenueStream) -> Dict:
    return {
        "id": stream.id,
        "stream_name": stream.stream_name,
        "stream_type": stream.stream_type,
        "base_units": float(stream.base_units or 0),
        "unit_price": float(stream.unit_price or 0),
        "growth_rate": float(stream.growth_rate or 0),
        "price_escalation": float(stream.price_escalation or 0),
        "churn_rate": float(stream.churn_rate or 0),
        "sort_order": stream.sort_order,
    }


def _default_driver_data() -> Dict[str, Any]:  # CRAFT-OK: dynamic driver_data JSON payload boundary (extra="allow" assumptions dict the engines read via .get)
    """Return sensible default driver data for a new scenario.

    Delegates to the typed ``LboAssumptions`` model (app/lbo/assumptions.py) so the
    model's named defaults are the SINGLE source of truth for a new scenario's
    assumptions (F-B4e Phase 3). The model's defaults are parity-pinned to the values
    this function used to return literally (see the assumptions CI golden).
    """
    from .assumptions import default_assumptions

    return default_assumptions()


def _get_drivers_dict(scenario_id: int, db) -> Dict[str, Any]:
    drivers_row = db.query(LboDriver).filter(LboDriver.scenario_id == scenario_id).first()
    driver_data = drivers_row.driver_data if drivers_row and drivers_row.driver_data else _default_driver_data()
    drivers: Dict[str, Any] = {}
    for key in DEAL_KEYS:
        value = driver_data.get(key)
        if value is not None:
            drivers[key] = value
    return drivers


def tranche_to_engine_payload(
    tranche: LboDebtTranche,
    *,
    funded_amount: float | None = None,
    amort_years: int | None = None,
    term_years: int | None = None,
) -> dict[str, object]:  # the engine's opaque TranchePayload convention (debt_schedule.py)
    """THE canonical LboDebtTranche → engine-payload mapping (chunk C1 AC-C1b).

    Every tranche→engine-dict call site consumes this ONE function — the six
    previously-drifted inline blocks (this module's ``_get_tranches_list`` plus five
    in app/routers/lbo.py) hardcoded ``cash_sweep_flag: False`` and dropped fields
    inconsistently (``fees_pct`` reached some ``compute_sources_uses`` calls and not
    others). The payload carries the REAL stored sweep / revolver / seniority fields,
    making the debt-schedule engine's sweep + revolver machinery reachable.

    The keyword overrides encode the only genuine per-site semantics:
      - ``funded_amount`` — the schedule-override path amortizes the grid row's
        actual Y0 debt balance; the S&U-override path substitutes a confirmed
        manual tranche amount. Default: the stored ``current_balance``.
      - ``amort_years`` — the schedule-override path derives years-to-maturity
        from ``maturity_date``. Default: the stored ``amort_years``.
      - ``term_years`` — model years to the tranche's contractual maturity
        (AC-C4 leg 1): the engine retires a tranche whose maturity falls inside
        the model horizon AT that maturity (debt_schedule.py maturity-aware
        repayment). Default ``None`` — the caller must anchor the term to ITS
        model window (engine_composition derives it from ``maturity_date`` vs
        ``model_start_year``); the serializer never fabricates one, so callers
        that pass no term keep the pre-existing horizon-bullet behavior.
    """
    return {
        "tranche_name": tranche.tranche_name,
        "funded_amount": (
            float(funded_amount) if funded_amount is not None
            else float(tranche.current_balance or 0)
        ),
        "interest_rate": float(tranche.sofr_rate or 0) + float(tranche.spread_bps or 0) / 10000,
        "fees_pct": float(tranche.fees_pct or 0),
        "amort_years": amort_years if amort_years is not None else tranche.amort_years,
        "term_years": int(term_years) if term_years is not None else None,
        "bullet_pct": float(tranche.bullet_pct or 0),
        "rate_type": "floating",
        "base_rate": float(tranche.sofr_rate or 0),
        "spread": float(tranche.spread_bps or 0) / 10000,
        "pik_flag": bool(tranche.pik_flag),
        "cash_sweep_flag": bool(tranche.cash_sweep_flag),
        "revolver_flag": bool(tranche.revolver_flag),
        "commitment": float(tranche.commitment or 0),
        "seniority_rank": (
            int(tranche.seniority_rank) if tranche.seniority_rank is not None else None
        ),
    }


def book_debt_equity_from_rows(rows: list[dict[str, object]]) -> dict[str, float]:
    """Extract the PRE-DEAL book debt + equity lines from the grid's Y0 input rows.

    The Book column of the S&U opening bridge (C2-c, plan 20260717-three-statement-spine
    AC-6) needs exactly the lines :func:`grid_helpers.historical_bs_from_rows` EXCLUDES
    by construction: historical debt (refinanced into the LBO tranches at close) and
    historical equity (eliminated under ASC 805). This is the companion extractor —
    same in-memory row shape ({section, row_type, line_code, label, driver_params,
    values}), same anchor rule (a row carrying ``driver_params["pre_deal_y0"]`` reads
    the FROZEN pre-deal anchor, never the live post-bridge PF value), so the two
    functions together reconstruct the COMPLETE pre-deal book balance sheet.

    Classification (line_code first, label/section fallback — never drop a line the
    sibling extractor deliberately skipped, or the bridge's Dr/Cr set cannot balance):
      * ``debt_std`` / ``debt_ltd`` → ``debt`` (also: a ``bs_liability`` input row
        with a debt-looking label and no recognized code — the same refinanced-debt
        set historical_bs_from_rows skips);
      * ``common_equity`` → ``common_equity``; ``retained_earnings`` →
        ``retained_earnings``; any other ``equity``-section input row sweeps into
        ``common_equity`` (residual paid-in line).
    """
    from .grid_helpers import PRE_DEAL_Y0_KEY, _looks_like_debt

    book = {"debt": 0.0, "common_equity": 0.0, "retained_earnings": 0.0}

    for row in rows:
        if (row.get("row_type") or "input") != "input":
            continue  # formula/subtotal rows (total_debt, total_equity) are derived
        code = str(row.get("line_code") or "").strip()
        section = str(row.get("section") or "")
        label = str(row.get("label") or "").lower()
        params = row.get("driver_params") or {}
        anchor = params.get(PRE_DEAL_Y0_KEY) if isinstance(params, dict) else None  # SA-OK: dict.get on an in-memory grid row's params, NOT an ORM query
        vals = row.get("values") or []
        live = vals[0] if isinstance(vals, list) and vals else None
        value = float(anchor if anchor is not None else (live or 0))

        if code in ("debt_std", "debt_ltd"):
            book["debt"] += value
        elif code == "common_equity":
            book["common_equity"] += value
        elif code == "retained_earnings":
            book["retained_earnings"] += value
        elif section == "bs_liability" and _looks_like_debt(label):
            book["debt"] += value  # the label-classified refinanced-debt residual
        elif section == "equity":
            book["common_equity"] += value  # residual equity input → paid-in bucket

    return {key: round(value, 2) for key, value in book.items()}


def _get_tranches_list(scenario_id: int, db) -> list[dict[str, object]]:
    # CROSS-TENANT-OK: scenario_id is tenant-verified by _require_lbo_scenario (check_entity_tenant_access) in every caller before this runs; LboDebtTranche has no tenant column beyond company_id, scenario-scoping is the file-wide pattern (unchanged pre-existing query, mirrors routers/lbo.py:3218)
    tranches_db = db.query(LboDebtTranche).filter(LboDebtTranche.scenario_id == scenario_id).all()
    return [tranche_to_engine_payload(tranche) for tranche in tranches_db]

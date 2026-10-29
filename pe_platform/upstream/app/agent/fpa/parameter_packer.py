"""Pure v2 compiler into the existing deterministic engine's canonical contract."""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, InvalidOperation

from ...lbo.sources_uses import DealBasisError, resolve_deal_basis
from .model_inputs import BUCKETS, ContractError, WorkbenchModelInputs
from .modes import CONSOLIDATED_ROLLUP_UNSUPPORTED, ForecastMode

IS = {"revenue", "contra_revenue", "cogs", "opex", "depreciation", "amortization",
      "other_income", "other_expense", "interest_expense", "income_tax_expense"}
CREDIT = {"revenue", "contra_revenue", "other_income", "accumulated_depreciation",
          "accounts_payable", "accrued_expenses", "other_current_liabilities",
          "short_term_debt", "long_term_debt", "common_equity", "retained_earnings"}
ASSETS = {"cash", "accounts_receivable", "inventory", "other_current_assets", "gross_fixed_assets",
          "intangibles", "goodwill", "other_assets"}
LIABILITIES = {"accounts_payable", "accrued_expenses", "other_current_liabilities", "short_term_debt", "long_term_debt"}


def _error(code, fields, message):
    raise ContractError(code, fields, message)


def _money(value, field):
    try:
        if isinstance(value, bool):
            raise ValueError()
        d = Decimal(str(value))
        if not d.is_finite():
            raise ValueError()
        return d.quantize(Decimal(".01"))
    except (ValueError, InvalidOperation):
        _error("invalid_model_inputs", [field], "Source amounts must be finite money values.")


def effective_accounting_view(session, inputs):
    """Reconcile independently of all engine plugs using debit-positive source amounts."""
    tb = session.trial_balance
    mappings = dict(inputs.account_mappings or {})
    mappings.update(session.resolved_classifications)
    unknown = set(mappings) - set(tb.accounts)
    if unknown or set(tb.balances) - set(tb.accounts):
        _error("invalid_model_inputs", sorted(unknown | (set(tb.balances) - set(tb.accounts))), "Unknown account reference.")
    totals = {b: Decimal(0) for b in BUCKETS}
    effective, missing = {}, []
    for aid, account in tb.accounts.items():
        balance = tb.balances.get(aid)
        if balance is None:
            _error("model_inputs_incomplete", [f"balances.{aid}"], "Each source account requires an explicit balance.")
        net = _money(balance.net_balance, f"balances.{aid}.net_balance")
        debit = _money(balance.debit_balance, f"balances.{aid}.debit_balance")
        credit = _money(balance.credit_balance, f"balances.{aid}.credit_balance")
        signed = net if account.normal_balance.value == "debit" else -net
        if (debit or credit) and debit - credit != signed:
            _error("invalid_model_inputs", [f"balances.{aid}"], "Debit/credit and normal-side-relative net balance disagree.")
        bucket = mappings.get(aid)
        if signed and not bucket:
            missing.append(f"account_mappings.{aid}")
            continue
        if bucket:
            amount = -signed if bucket in CREDIT else signed
            totals[bucket] += amount
            effective[aid] = {"bucket": bucket, "amount": float(amount), "signed_debit": float(signed)}
    if missing:
        _error("model_inputs_incomplete", missing, "Map every nonzero account exactly once.")
    revenue = totals["revenue"] + totals["contra_revenue"]
    op = revenue - totals["cogs"] - totals["opex"] - totals["depreciation"] - totals["amortization"]
    earnings = op + totals["other_income"] - totals["other_expense"] - totals["interest_expense"] - totals["income_tax_expense"]
    assets = sum(totals[b] for b in ASSETS) - totals["accumulated_depreciation"]
    liabilities = sum(totals[b] for b in LIABILITIES)
    equity = totals["common_equity"] + totals["retained_earnings"]
    current_earnings = earnings if inputs.tb_presentation == "unclosed" else Decimal(0)
    residual = assets - liabilities - equity - current_earnings
    if abs(residual) > Decimal(".01"):
        _error("source_balance_sheet_unreconciled", ["tb_presentation", "account_mappings", "trial_balance"],
               f"Source assets minus liabilities and equity (including declared current earnings) is {residual}.")
    return effective, {k: float(v) for k, v in totals.items()}, {
        "assets": float(assets), "liabilities": float(liabilities), "equity_before_current_earnings": float(equity),
        "current_earnings": float(current_earnings), "residual": float(residual),
        "presentation": inputs.tb_presentation, "formula": "assets - liabilities - source_equity - unclosed_current_earnings",
        "period": tb.as_of_date.isoformat(), "kind": "source_derived",
    }


def pack_interview_parameters(session, num_forecast_years=5):
    if session.schema_version != 2:
        _error("interview_schema_upgrade_required", [], "Legacy interviews require a fresh v2 interview.")
    if session.mode == ForecastMode.CONSOLIDATED_ROLLUP:
        _error("unsupported_model_inputs", ["mode"], CONSOLIDATED_ROLLUP_UNSUPPORTED)
    if type(num_forecast_years) is not int or not 1 <= num_forecast_years <= 10:
        _error("invalid_model_inputs", ["num_years"], "Forecast horizon must be 1 through 10 years.")
    pending = [q.question_id for q in session.questions if not q.is_answered]
    if pending:
        _error("interview_incomplete", pending, "Accept each material interview decision before computing.")
    if session.trial_balance is None:
        _error("model_inputs_incomplete", ["trial_balance"], "A source trial balance is required.")
    # Rebuild execution inputs from the current accepted decision map on a copy;
    # cached derived dictionaries are display conveniences, never authority.
    from .interview_engine import _rebuild
    session = deepcopy(session)
    _rebuild(session)
    inputs = WorkbenchModelInputs.model_validate(session.model_inputs)
    required = ["tb_presentation", "account_mappings", "operating_cost_policy", "other_income_expense_policy",
        "ar_dso", "ap_dpo", "inventory_dio", "other_ca_pct", "accrued_pct", "other_liab_pct", "tax_rate",
        "distribution_pct_of_fcf", "sweep_pct", "debt_facilities", "assets"]
    missing = [f for f in required if getattr(inputs, f) is None]
    missing += [f"decisions.{k}" for k in ("revenue_growth_cagr", "capex_pct", "min_cash_floor") if k not in session.driver_overrides]
    if missing:
        _error("model_inputs_incomplete", missing, "Explicit accepted inputs are required; unknown does not mean zero.")
    if inputs.sweep_pct_by_year is not None and (len(inputs.sweep_pct_by_year) != num_forecast_years or
            any(not 0 <= v <= 1 for v in inputs.sweep_pct_by_year)):
        _error("invalid_model_inputs", ["sweep_pct_by_year"], "Supply one bounded sweep percentage per forecast year.")
    effective, totals, reconciliation = effective_accounting_view(session, inputs)
    revenue = totals["revenue"] + totals["contra_revenue"]
    if revenue <= 0:
        _error("unsupported_model_inputs", ["operating_cost_policy", "revenue"], "The supported aggregate-ratio model requires positive revenue, not positive EBITDA.")
    cogs, opex = totals["cogs"], totals["opex"]
    da = totals["depreciation"] + totals["amortization"]
    operating_income = round(revenue - cogs - opex - da, 2)
    reported_ebitda = round(operating_income + da, 2)
    total_addbacks = 0.0
    for addback in session.ebitda_addbacks:
        source = effective.get(addback["account_id"])
        if not source or source["bucket"] not in ("cogs", "opex") or source["amount"] <= 0:
            _error("unsupported_model_inputs", [f"ebitda_addback:{addback['account_id']}"], "Only an identified positive operating cost can be adjusted; D&A cannot be added twice.")
        amount = round(source["amount"] * addback["pct"], 2)
        total_addbacks += amount
        if addback["treatment"] == "remove_recurring_cost":
            if source["bucket"] == "cogs":
                cogs -= amount
            else:
                opex -= amount
    normalized = round(reported_ebitda + total_addbacks, 2)
    historical_bs = {b: totals[b] for b in ASSETS | LIABILITIES | {"accumulated_depreciation", "common_equity", "retained_earnings"}}
    historical_bs["retained_earnings"] = round(totals["retained_earnings"] + reconciliation["current_earnings"], 2)
    historical_bs["net_fixed_assets"] = round(totals["gross_fixed_assets"] - totals["accumulated_depreciation"], 2)
    historical_bs["total_debt"] = round(totals["short_term_debt"] + totals["long_term_debt"], 2)
    historical_bs["equity_contributed"] = totals["common_equity"]
    if inputs.inventory_dio == 0 and totals["inventory"] != 0:
        _error("unsupported_model_inputs", ["inventory_dio", "account_mappings.inventory"],
               "Zero DIO with opening inventory is unsupported: this engine would carry inventory flat rather than release it. Supply a positive supported days assumption or use a separately reviewed inventory model.")
    if totals["accumulated_depreciation"] < 0:
        _error("unsupported_model_inputs", ["account_mappings.accumulated_depreciation"],
               "A debit (negative) opening accumulated depreciation balance is unsupported: the engine carries accumulated depreciation as a credit contra-asset.")
    # Every nonzero opening asset must have a supported policy, not implicit carrying behavior.
    optional_policies = []
    if totals["intangibles"]:
        optional_policies.append("intangible_amort_years")
    if totals["goodwill"] or session.mode == ForecastMode.TRANSACTION_LBO:
        optional_policies += ["goodwill_book_amort_years", "goodwill_tax_amort_years", "section_197_election"]
    missing = [k for k in optional_policies if getattr(inputs, k) is None]
    if missing:
        _error("model_inputs_incomplete", missing, "State the supported opening-asset amortization policy explicitly.")

    streams, margins, stream_refs = [], {}, {}
    for aid, line in effective.items():
        if line["bucket"] in ("revenue", "contra_revenue") and line["amount"]:
            stream = "account_" + aid.encode("utf-8").hex()
            streams.append(stream)
            margins[stream + "_base"] = line["amount"]
            stream_refs[stream] = aid
    growth = session.driver_overrides["revenue_growth_cagr"]
    drivers = {"mode": session.mode.value, "revenue_streams": streams,
        "revenue_growth_by_stream": {s: [growth] * num_forecast_years for s in streams + ["overall"]},
        "margin_by_stream": margins, "direct_cost_pct": cogs / revenue, "facilities_pct": 0.0,
        "other_ga_pct": opex / revenue, "min_cash_floor": session.driver_overrides["min_cash_floor"],
        "sweep_pct_by_year": inputs.sweep_pct_by_year if inputs.sweep_pct_by_year is not None else [inputs.sweep_pct] * num_forecast_years,
        "other_income_by_year": [totals["other_income"]] * num_forecast_years,
        "other_expense_by_year": [totals["other_expense"]] * num_forecast_years,
        "entry_ebitda_multiple": 0.0, "transaction_fees_pct": 0.0}
    for k in ("ar_dso", "ap_dpo", "inventory_dio", "other_ca_pct", "accrued_pct", "other_liab_pct", "tax_rate",
              "distribution_pct_of_fcf", "sweep_pct", *optional_policies):
        drivers[k] = getattr(inputs, k)
    capex = []
    seen = set()
    for i, asset in enumerate(inputs.assets):
        if asset.category in seen or asset.depreciation_method != "engine_vintage_straight_line":
            _error("unsupported_model_inputs", [f"assets.{i}"], "Use unique categories and explicitly acknowledge the engine vintage straight-line convention.")
        if asset.beginning_accum_depr > asset.beginning_gross:
            _error("invalid_model_inputs", [f"assets.{i}.beginning_accum_depr"], "Accumulated depreciation cannot exceed gross assets.")
        if asset.remaining_life_years is not None and asset.remaining_life_years > asset.useful_life_years:
            _error("invalid_model_inputs", [f"assets.{i}.remaining_life_years"], "Remaining life cannot exceed the useful life.")
        if asset.beginning_accum_depr:
            # Opening net book value: the engine depreciates (gross less salvage less
            # accumulated depreciation) straight-line over the stated remaining life and
            # keeps gross and accumulated depreciation separate on every projected year.
            depreciable = round(asset.beginning_gross * (1 - asset.salvage_value_pct) - asset.beginning_accum_depr, 2)
            if depreciable < 0:
                _error("invalid_model_inputs", [f"assets.{i}.beginning_accum_depr", f"assets.{i}.salvage_value_pct"],
                       "Accumulated depreciation exceeds the depreciable base (gross assets less salvage value).")
            if depreciable > 0 and asset.remaining_life_years is None:
                _error("model_inputs_incomplete", [f"assets.{i}.remaining_life_years"],
                       "A category with opening accumulated depreciation needs remaining_life_years: the years left to depreciate its opening net book value (gross less salvage less accumulated depreciation).")
        seen.add(asset.category)
        capex_driver = asset.model_dump(exclude={"depreciation_method"})
        if asset.beginning_accum_depr:
            capex_driver["opening_basis"] = "net_book_value"
        capex.append(capex_driver)
    for key, expected in [("beginning_gross", totals["gross_fixed_assets"]),
                          ("beginning_accum_depr", totals["accumulated_depreciation"]),
                          ("capex_pct_revenue", session.driver_overrides["capex_pct"])]:
        if abs(sum(c[key] for c in capex) - expected) > (1e-9 if key == "capex_pct_revenue" else .005):
            _error("model_inputs_incomplete", [f"assets.{key}"], "Asset categories must exactly cover source assets and accepted capex rate.")

    tranches, ids, linked = [], set(), set()
    treatments = {t["account_id"]: t["treatment"] for t in session.debt_tranche_definitions}
    is_lbo = session.mode == ForecastMode.TRANSACTION_LBO
    for i, debt in enumerate(inputs.debt_facilities):
        field = f"debt_facilities.{i}"
        if debt.facility_id in ids or (debt.account_id and debt.account_id in linked):
            _error("invalid_model_inputs", [field], "Duplicate facility or source debt account.")
        ids.add(debt.facility_id)
        if debt.account_id:
            source = effective.get(debt.account_id)
            if not source or source["bucket"] not in ("short_term_debt", "long_term_debt") or abs(source["amount"] - debt.opening_balance) > .005:
                _error("invalid_model_inputs", [field + ".account_id"], "Retained facility must match the exact mapped source balance.")
            linked.add(debt.account_id)
        elif not is_lbo and debt.opening_balance:
            _error("unsupported_model_inputs", [field], "An operating opening debt balance requires a source account.")
        if debt.opening_balance != debt.funded_amount:
            _error("unsupported_model_inputs", [field], "This contract supports retained operating debt or at-close funding with equal opening and funded amounts.")
        if not is_lbo and debt.fees_pct:
            _error("unsupported_model_inputs", [field + ".fees_pct"], "New operating financing fees are not modeled by this engine path.")
        if debt.commitment < debt.opening_balance:
            _error("invalid_model_inputs", [field + ".commitment"], "Opening debt exceeds the stated commitment.")
        if debt.rate_type == "fixed" and (debt.base_rate or debt.spread) or debt.rate_type == "floating" and debt.interest_rate:
            _error("invalid_model_inputs", [field + ".rate_type"], "Only the selected fixed or floating rate components may be nonzero.")
        if not debt.revolver_flag and debt.commitment_fee_pct:
            _error("unsupported_model_inputs", [field + ".commitment_fee_pct"], "Commitment fees are supported only on revolving facilities.")
        if debt.amort_years and debt.amort_basis <= 0:
            _error("invalid_model_inputs", [field + ".amort_basis"], "Amortization requires a positive contractual basis.")
        if debt.revolver_flag and (debt.amort_years or debt.bullet_pct or debt.pik_flag or debt.term_years is not None):
            _error("unsupported_model_inputs", [field], "Revolver maturity/amortization/PIK is not supported by this engine.")
        if debt.bullet_pct and debt.term_years != num_forecast_years:
            _error("unsupported_model_inputs", [field + ".bullet_pct"], "A horizon bullet is supported only at the stated horizon maturity.")
        treatment = treatments.get(debt.account_id)
        if treatment == "interest_only" and (debt.amort_years or debt.sweep_participating):
            _error("invalid_model_inputs", [field], "Interest-only choice conflicts with amortization or cash sweep.")
        if treatment == "accelerated" and (not debt.sweep_participating or not any(drivers["sweep_pct_by_year"])):
            _error("invalid_model_inputs", [field + ".sweep_participating"], "Acceleration requires explicit sweep participation.")
        if treatment == "contractual" and not debt.amort_years and debt.term_years is None and not debt.revolver_flag:
            _error("model_inputs_incomplete", [field + ".amort_years", field + ".term_years"], "Contractual treatment requires an amortization or maturity term.")
        if debt.term_years == num_forecast_years and (debt.pik_flag or debt.bullet_pct != 1):
            _error("unsupported_model_inputs", [field + ".bullet_pct"], "Horizon maturity requires a full principal bullet; PIK at this boundary is unsupported.")
        if debt.term_years == num_forecast_years and debt.amort_basis < debt.opening_balance:
            _error("unsupported_model_inputs", [field + ".amort_basis", field + ".term_years"],
                   "The horizon bullet basis must cover the full opening principal; a smaller basis can leave debt outstanding at maturity.")
        if is_lbo and debt.fees_pct:
            # The engine amortizes OID on an independent straight-line schedule.
            # Accept only shapes whose principal cannot retire before that OID.
            # Never clamp carrying debt or accelerate OID after the fact.
            matched_amortization = (
                debt.amort_years is not None and debt.amort_years > 0
                and debt.amort_years.is_integer()
                and debt.amort_basis == debt.opening_balance
                and (debt.term_years is None or debt.term_years >= debt.amort_years)
            )
            matched_horizon_bullet = (
                debt.amort_years is None and debt.amort_basis == debt.opening_balance
                and debt.term_years == num_forecast_years and debt.bullet_pct == 1
            )
            if (debt.revolver_flag or debt.pik_flag or debt.sweep_participating
                    or not (matched_amortization or matched_horizon_bullet)):
                _error("unsupported_model_inputs", [field + ".fees_pct", field + ".amort_years",
                       field + ".amort_basis", field + ".term_years", field + ".sweep_participating"],
                       "Financing fees require matched integer-year principal/OID amortization with no earlier maturity, or an interest-only full-horizon bullet. Fee-bearing revolvers, PIK, cash sweeps and unmatched repayment horizons are unsupported.")
            # Cent-rounded scheduled payments can retire very small principals
            # sooner than the nominal amortization years. Use the unchanged pure
            # schedule primitives to verify this accepted subset before allowing
            # an independent OID vector to outlive even one cent of principal.
            from ...lbo.debt_schedule import _init_tranche_state, _tranche_year_flows
            from ...lbo.sources_uses import oid_amortization_vectors
            state = _init_tranche_state([debt.model_dump()])[0]
            oid = oid_amortization_vectors(debt.funded_amount, debt.fees_pct, debt.amort_years, num_forecast_years)
            for year in range(num_forecast_years):
                flows = _tranche_year_flows(state, year, num_forecast_years)
                state["balance"] = round(max(0., state["balance"] - flows["amort"] - flows["bullet"]), 2)
                if oid["remaining"][year] > state["balance"]:
                    _error("unsupported_model_inputs", [field + ".fees_pct", field + ".amort_years", field + ".amort_basis"],
                           "Cent-rounded principal repayment would leave unamortized financing fees above outstanding debt; this repayment/OID shape is unsupported.")
        tranches.append(debt.model_dump(exclude={"facility_id", "account_id"}))
    debt_accounts = {aid for aid, v in effective.items() if v["bucket"] in ("short_term_debt", "long_term_debt") and v["amount"]}
    if debt_accounts - linked:
        _error("model_inputs_incomplete", ["debt_facilities." + a for a in sorted(debt_accounts - linked)], "Every opening debt account requires explicit facility terms.")
    if not is_lbo and abs(sum(t["opening_balance"] for t in tranches) - historical_bs["total_debt"]) > .005:
        _error("invalid_model_inputs", ["debt_facilities"], "Retained facility openings must equal source debt.")
    if is_lbo:
        transaction = inputs.transaction
        if transaction is None:
            _error("model_inputs_incomplete", ["transaction"], "Explicit transaction price and funding policy are required.")
        if transaction.shape != "debt_free_target_book_assets" or debt_accounts or totals["goodwill"]:
            _error("unsupported_model_inputs", ["transaction.shape", "debt_treatment", "goodwill"], "Only a debt-free target without legacy goodwill is supported; retained/refinanced debt and debt-to-equity conversion need a separate compiler contract.")
        if transaction.valuation_basis == "fixed_enterprise_value":
            if transaction.entry_enterprise_value is None or transaction.entry_enterprise_value <= 0 or transaction.entry_ebitda_multiple is not None:
                _error("invalid_model_inputs", ["transaction"], "A positive fixed EV and null multiple are required.")
            drivers["entry_enterprise_value"] = transaction.entry_enterprise_value
            ev = transaction.entry_enterprise_value
        else:
            if normalized <= 0 or not transaction.entry_ebitda_multiple or transaction.entry_enterprise_value is not None:
                _error("unsupported_model_inputs", ["transaction.valuation_basis"], "Multiple valuation requires positive EBITDA and an explicit positive multiple.")
            drivers["entry_ebitda_multiple"] = transaction.entry_ebitda_multiple
            ev = normalized * transaction.entry_ebitda_multiple
        drivers["deal_basis"] = transaction.deal_basis
        drivers["minimum_operating_cash"] = transaction.minimum_operating_cash
        try:
            basis = resolve_deal_basis(drivers, historical_bs, historical_bs["total_debt"])
        except DealBasisError as exc:
            _error("invalid_model_inputs", ["transaction.deal_basis", "transaction.minimum_operating_cash"], str(exc))
        # EV prices the business excluding cash on either basis (an equity purchase pays
        # for the acquired cash as a separate Use), so goodwill is EV less identifiable
        # net assets excluding the target's book cash.
        identifiable_net_assets = round(reconciliation["assets"] - totals["cash"] - reconciliation["liabilities"], 2)
        transaction_goodwill = round(ev - identifiable_net_assets, 2)
        if transaction_goodwill < 0:
            _error("unsupported_model_inputs", ["transaction.entry_enterprise_value", "transaction.entry_ebitda_multiple", "transaction.valuation_basis"],
                   "Bargain purchases are unsupported: accepted enterprise value is below source-reconciled identifiable net assets (excluding cash) and would create negative goodwill in this engine.")
        drivers["transaction_fees_pct"] = transaction.transaction_fees_pct
        uses = (ev * (1 + transaction.transaction_fees_pct) + sum(t["funded_amount"] * t["fees_pct"] for t in tranches)
                + basis["cash_to_balance_sheet"])
        if sum(t["funded_amount"] for t in tranches) > uses:
            _error("unsupported_model_inputs", ["transaction.equity_funding_policy"], "Negative residual equity funding is unsupported.")
    elif inputs.transaction is not None:
        _error("unsupported_model_inputs", ["transaction"], "Transaction inputs cannot be applied in operating mode.")

    return {"computation_contract_version": 2, "entity_id": session.entity_id, "num_years": num_forecast_years,
        "base_revenue": revenue, "reported_operating_income": operating_income, "identified_da": da,
        "reported_ebitda": reported_ebitda, "total_addbacks": round(total_addbacks, 2), "normalized_ebitda": normalized,
        "drivers": drivers, "debt_tranches": tranches, "capex_drivers": capex, "payroll_drivers": [],
        "historical_bs": historical_bs, "forecast_opening_bs": None if is_lbo else deepcopy(historical_bs),
        "ebitda_addbacks_detail": deepcopy(session.ebitda_addbacks), "debt_tranches_detail": deepcopy(session.debt_tranche_definitions),
        "resolved_classifications": deepcopy(session.resolved_classifications), "effective_accounts": effective,
        "source_reconciliation": reconciliation, "stream_account_refs": stream_refs,
        "provenance": {"assumptions": deepcopy(session.input_provenance), "decisions": deepcopy(session.decisions),
            "derived": {"kind": "source_derived", "period": session.trial_balance.as_of_date.isoformat(),
                "formulas": {"reported_ebitda": "revenue - cogs - opex", "direct_cost_pct": "recurring_cogs / revenue",
                             "other_ga_pct": "recurring_opex / revenue", "normalized_ebitda": "reported_ebitda + accepted_addbacks"}}},
        "disclosures": ["Aggregate recurring COGS/OpEx ratios; facilities and payroll are not separately modeled.",
            "Asset engine convention: new vintages depreciate beginning next year; an opening category with a stated remaining life (required when it carries accumulated depreciation) depreciates its net book value (gross less salvage less accumulated depreciation) over that life; without one, gross less salvage depreciates over max(life - 1, 1). Gross assets and accumulated depreciation are carried separately.",
            "Zero DIO is supported only with zero opening inventory; positive DIO models inventory and its working-capital cash movement.",
            "Transaction bargain purchases and unmatched financing-fee repayment horizons are refused before engine execution.",
            "Valuation-only adjustments do not change projected costs or create projected addback lines.",
            "Liquidity shortfalls are disclosed by the engine; no credit facility is invented."]
            + (["Transaction deal basis: " + drivers["deal_basis"] + ". On a cash-free/debt-free deal the seller keeps the target's book cash and the opening cash is the stated minimum operating cash, funded as a Use; an equity purchase pays for the acquired cash as a Use."]
               if is_lbo else [])}

"""
app/lbo/working_capital.py
──────────────────────────
Working Capital engine: AR, AP, Inventory (DIO), Other Current Assets/Liabilities,
Accrued Expenses.
"""
from typing import Dict, Any, List


def compute_working_capital(
    drivers: Dict[str, Any],
    revenue_by_year: List[float],
    cogs_by_year: List[float],
    opex_by_year: List[float],
    opening_wc: dict[str, float] | None = None,
    level_revenue_by_year: list[float] | None = None,
    level_cogs_by_year: list[float] | None = None,
    level_opex_by_year: list[float] | None = None,
) -> Dict[str, Any]:
    """
    Compute working capital schedule over the projection period.

    Args:
        drivers: Main drivers (ar_dso, ap_dpo, other_ca_pct, accrued_pct,
            other_liab_pct, inventory_dio)
        revenue_by_year: Revenue per projection year
        cogs_by_year: COGS per projection year
        opex_by_year: Total operating expenses per year
        opening_wc: Opening WC balances {ar, ap, inventory, other_ca, accrued, other_cl}

    Returns:
        Per-year WC balances and changes

    Inventory / DIO (C2-b, plan 20260717-three-statement-spine AC-5 — closes the
    disclosed grid-vs-engine divergence DL-3): ``inventory_dio`` > 0 activates
    the days framework — inventory = COGS × DIO/365 per year, the year-0 delta
    measured against the REAL opening inventory, and Δinventory joins ΔNWC (an
    asset build is a cash use). Absent / 0 (every stored scenario) → OFF: the
    schedule rows carry no inventory keys and every number is byte-identical to
    the pre-existing behavior (the engine BS keeps its static inventory carry).
    """
    ar_dso = float(drivers.get("ar_dso", 30))
    ap_dpo = float(drivers.get("ap_dpo", 45))
    other_ca_pct = float(drivers.get("other_ca_pct", 0.02))
    accrued_pct = float(drivers.get("accrued_pct", 0.05))
    other_liab_pct = float(drivers.get("other_liab_pct", 0.02))
    inventory_dio = float(drivers.get("inventory_dio", 0) or 0)
    dio_active = inventory_dio > 0

    owc = opening_wc or {}
    prev_ar = float(owc.get("ar", 0))
    prev_ap = float(owc.get("ap", 0))
    prev_other_ca = float(owc.get("other_ca", 0))
    prev_accrued = float(owc.get("accrued", 0))
    prev_other_cl = float(owc.get("other_cl", 0))
    prev_inventory = float(owc.get("inventory", 0))

    years = []

    for i, rev in enumerate(revenue_by_year):
        cogs = cogs_by_year[i] if i < len(cogs_by_year) else 0
        opex = opex_by_year[i] if i < len(opex_by_year) else 0
        # W3d (CORR-2): point-in-time WC LEVELS come off the ANNUALIZED run-rate (level_* vectors). A stub
        # year feeds the f-scaled RECOGNIZED flow in rev/cogs/opex (→ the P&L), but the balance-sheet STOCK
        # (AR = annual-rev × DSO/365) must reflect the FULL run-rate, not the partial-period recognized amount.
        # All SIX WC lines annualize (ar/other_ca off rev, ap/inventory off cogs, accrued/other_cl off opex).
        # Default None → the level IS the flow → byte-identical to the pre-W3d behavior.
        rL = level_revenue_by_year[i] if level_revenue_by_year is not None else rev
        cL = level_cogs_by_year[i] if level_cogs_by_year is not None else cogs
        oL = level_opex_by_year[i] if level_opex_by_year is not None else opex

        # End-of-period balances (off the annualized levels; the deltas below still difference REAL levels)
        ar = round(rL * ar_dso / 365, 2)
        ap = round(cL * ap_dpo / 365, 2)
        other_ca = round(rL * other_ca_pct, 2)
        accrued = round(oL * accrued_pct, 2)
        other_cl = round(oL * other_liab_pct, 2)
        inventory = round(cL * inventory_dio / 365, 2) if dio_active else 0.0

        # Changes (increase in asset = cash use, increase in liability = cash source)
        delta_ar = round(ar - prev_ar, 2)
        delta_ap = round(ap - prev_ap, 2)
        delta_other_ca = round(other_ca - prev_other_ca, 2)
        delta_accrued = round(accrued - prev_accrued, 2)
        delta_other_cl = round(other_cl - prev_other_cl, 2)
        delta_inventory = round(inventory - prev_inventory, 2) if dio_active else 0.0

        # Net WC change (negative = cash generation)
        # Increase in assets is cash use (+), increase in liabilities is cash source (-)
        delta_nwc = round(
            delta_ar + delta_inventory + delta_other_ca
            - delta_ap - delta_accrued - delta_other_cl,
            2,
        )

        row = {
            "year_index": i,
            "ar": ar,
            "ap": ap,
            "other_ca": other_ca,
            "accrued": accrued,
            "other_cl": other_cl,
            "net_working_capital": round(
                ar + inventory + other_ca - ap - accrued - other_cl, 2
            ),
            "delta_ar": delta_ar,
            "delta_ap": delta_ap,
            "delta_other_ca": delta_other_ca,
            "delta_accrued": delta_accrued,
            "delta_other_cl": delta_other_cl,
            "delta_nwc": delta_nwc,
        }
        if dio_active:
            # Inventory keys appear ONLY when the DIO driver is on, so an
            # off-path schedule row is byte-identical to the pre-C2-b shape.
            row["inventory"] = inventory
            row["delta_inventory"] = delta_inventory
        years.append(row)

        prev_ar, prev_ap = ar, ap
        prev_other_ca, prev_accrued, prev_other_cl = other_ca, accrued, other_cl
        prev_inventory = inventory

    return {
        "schedule": years,
        # Labeled driver decision (AC-5: every new regime/driver is labeled in
        # serialized output) — the DIO days framework state for this run.
        "inventory_dio_active": dio_active,
        "inventory_dio": inventory_dio if dio_active else None,
    }

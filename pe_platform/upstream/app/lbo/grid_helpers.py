from __future__ import annotations

from typing import Any, Dict, List

from typing import TypedDict

from sqlalchemy.orm import Session

from ..models import LboCell, LboPeriod
from .post_close_band import BAND_COL_BASE  # the three-range col_index invariant: <0 hist · 1..N positional · >=BAND_COL_BASE band


# 7 POSITIVE columns (plan 20260718-fycy-absolute-axis, AC-1): TTM (seq 0) ·
# FYCY (seq 1) · FY+1..FY+5 (seq 2..6). Coupled to qbo_seed.FORECAST_YEAR_COUNT by
# the invariant NUM_YEAR_COLS == FORECAST_YEAR_COUNT + 1 (col 1 is the seq-0 TTM
# anchor; cols 2..7 are the 6 forecast columns). grid_qbo.py's init-persist loop
# reads periods_by_seq[ci] for ci in range(NUM_YEAR_COLS) — bumping this without
# bumping FORECAST_YEAR_COUNT raises KeyError on periods_by_seq[6] and 500s init.
NUM_YEAR_COLS = 7


def positional_period_label(col_index: int) -> str:
    """FYCY-axis label for a bare positional ``col_index`` when no ``LboPeriod``
    row is loaded for the cell (plan 20260718-fycy-absolute-axis, AC-AXIS-7).

    The authoritative labels are produced by ``build_period_spine``
    (``qbo_seed.py``): the positive axis is ``seq == col_index − 1`` — col 1 = TTM
    (seq 0), col 2 = FYCY (seq 1), col k≥3 = ``FY+{k−2}`` (seq k−1). ``col_index``
    ≤ 0 is the label column / a historical fiscal year whose absolute-year label
    (e.g. "2021") lives ONLY on the period row — a positional reader cannot
    reconstruct it, so it degrades to a generic marker. A period-keyed cell must
    read ``period.label`` directly; this is the degrade path for a legacy /
    un-backfilled positional cell only, and it never disagrees with
    ``build_period_spine`` on the positive axis.
    """
    if col_index <= 0:
        return "historical"
    if col_index == 1:
        return "TTM"
    if col_index == 2:
        return "FYCY"
    return f"FY+{col_index - 2}"


# PLAN-EXEMPT: AC-5 extraction-completeness refinement (Steven-directed 2026-06-25, option A —
# purchase accounting). grid_helpers.py is one file beyond plan 20260625-pilot-bugs-batch-2's
# declared_scopes but squarely within AC-5's ratified "opening BS reconciles" intent: the opening
# BS only ties meaningfully if the historical BS it draws from is COMPLETE.

# Identifiable-asset and operating-liability slots, keyed off the standardized ``line_code``
# (standard_grid_builder + the COA-driven init set it). Historical DEBT (debt_std/debt_ltd) is
# EXCLUDED — an EV-based LBO refinances existing debt into the new tranches, so carrying it would
# double-count. Historical EQUITY is EXCLUDED — purchase accounting eliminates the target's equity
# at close; the new transaction goodwill is the balancing plug (see compute_sources_uses).
_HIST_BS_ASSET_CODES = {
    "cash": "cash",
    "ar": "accounts_receivable",
    "inventory": "inventory",
    "other_ca": "other_current_assets",
    "ppe": "net_fixed_assets",          # the grid carries NET PP&E directly
    "intangibles": "intangibles",
}
_HIST_BS_LIAB_CODES = {
    "ap": "accounts_payable",
    "accrued_liab": "accrued_expenses",
    "other_cl": "other_current_liabilities",
}
_HIST_BS_DEBT_CODES = {"debt_std", "debt_ltd"}  # refinanced into the LBO tranches → excluded

# AC-C3: the frozen pre-deal book anchor the PF-close bridge stamps into a row's
# driver_params. Once the bridge persists PF values into the grid's Y0 column,
# the pre-deal book stays recoverable ONLY through this key — the historical-BS
# extractor prefers it, so the goodwill plug never feeds on its own output
# (the corruption loop: PF intangibles carrying goodwill would otherwise inflate
# "identifiable assets" on the next recalc and shrink the plug every pass).
PRE_DEAL_Y0_KEY = "pre_deal_y0"

# The rows whose Y0 the bridge REWRITES to the PF opening position (anchor frozen
# at first bridge; a later Y0 edit on these rows is overwritten by the bridge —
# post-close they are transaction-derived, not book inputs). The debt rows
# (_HIST_BS_DEBT_CODES) are also transaction-set, but their PF Y0 is assigned by
# the tranche→row mapping in the router's schedule-override pass, not here.
_PF_TRANSACTION_CODES = ("cash", "intangibles", "common_equity", "retained_earnings")

# The standard-template skeleton the bridge requires. A grid missing these slots
# (a legacy v1 grid, a minimal test fixture) is not the standard 3-statement
# model — the bridge no-ops rather than mangling an unrecognized structure.
_PF_SKELETON_CODES = frozenset(
    {"balance_check", "total_assets", "total_liab_equity",
     "intangibles", "common_equity", "retained_earnings"}
)

# Working-capital alias keys (working_capital.py reads ar/ap/accrued/other_ca/other_cl off the
# opening BS); mirrored from their canonical counterparts after capture.
_HIST_BS_ALIASES = {
    "accounts_receivable": "ar",
    "accounts_payable": "ap",
    "accrued_expenses": "accrued",
    "other_current_assets": "other_ca",
    "other_current_liabilities": "other_cl",
}


def _looks_like_debt(label: str) -> bool:
    return any(k in label for k in ("debt", "loan", "line of credit", "note payable", "notes payable"))


def historical_bs_from_rows(rows: list[dict]) -> dict[str, float]:
    """Construct the COMPLETE historical opening balance sheet from the grid's Y0 input rows.

    The LBO opening BS only ties *meaningfully* if the historical BS it is built from is complete
    — a real QBO balance sheet ties (A = L + E), so dropping line items here forces the downstream
    goodwill plug to silently absorb the gap (a forced tie that validates nothing). This captures
    every identifiable asset + operating liability: it keys off the standardized ``line_code`` slot
    first, falls back to label/section classification, and sweeps any unrecognized line into the
    residual bucket for its section so NOTHING is dropped. Historical debt + equity are excluded by
    construction (purchase accounting).

    PURE over in-memory row dicts ({section, row_type, line_code, label, driver_params, values})
    so recalc-v2's PF bridge and the DB loader below share ONE classification. The PRE-DEAL
    anchor rule (AC-C3): a row carrying ``driver_params["pre_deal_y0"]`` reads the ANCHOR, not
    the live Y0 value — after the bridge persists PF Y0 values (goodwill folded into
    intangibles), the live value is post-close and would corrupt the plug's identifiable-asset
    base. Rows without an anchor (the book-carried lines) read the live, user-editable Y0.
    """
    historical_bs: dict[str, float] = {}

    def _add(key: str, value: float) -> None:
        historical_bs[key] = historical_bs.get(key, 0.0) + value

    for row in rows:
        if (row.get("row_type") or "input") != "input":
            continue  # formula/subtotal/data(child) rows never feed the historical BS
        section = str(row.get("section") or "")
        if not section.startswith("bs"):
            continue  # the LBO "debt" + "equity" sections are excluded by construction
        code = str(row.get("line_code") or "").strip()
        label = str(row.get("label") or "").lower()
        params = row.get("driver_params") or {}
        anchor = params.get(PRE_DEAL_Y0_KEY) if isinstance(params, dict) else None  # SA-OK: dict.get on an in-memory grid row's params, NOT an ORM query
        vals = row.get("values") or []
        live = vals[0] if vals else None
        value = float(anchor if anchor is not None else (live or 0))

        if code in _HIST_BS_DEBT_CODES:
            continue
        if code in _HIST_BS_ASSET_CODES:
            _add(_HIST_BS_ASSET_CODES[code], value)
            continue
        if code in _HIST_BS_LIAB_CODES:
            _add(_HIST_BS_LIAB_CODES[code], value)
            continue

        # No recognized line_code → classify by label + section; never drop.
        if "cash" in label:
            _add("cash", value)
        elif "receivable" in label:
            _add("accounts_receivable", value)
        elif "inventor" in label:
            _add("inventory", value)
        elif "accru" in label:
            _add("accrued_expenses", value)
        elif "payable" in label:
            _add("accounts_payable", value)
        elif section == "bs_liability":
            if _looks_like_debt(label):
                continue  # historical debt without a debt line_code — still refinanced
            _add("other_current_liabilities", value)   # residual operating liability
        elif section == "bs_fixed":
            _add("other_assets", value)                # residual long-term asset
        else:  # bs_asset residual (current asset)
            _add("other_current_assets", value)

    for canonical, alias in _HIST_BS_ALIASES.items():
        if canonical in historical_bs:
            historical_bs[alias] = historical_bs[canonical]

    return historical_bs


def _get_historical_bs(scenario_id: int, db: Session) -> dict[str, float]:
    """DB loader for :func:`historical_bs_from_rows` — reads the Y0 (col 1) input
    cells and delegates to the ONE pure classification (anchor-aware, AC-C3)."""
    # CROSS-TENANT-OK: scenario-scoped read; scenario_id is tenant-authorized upstream by the
    # route's _require_lbo_scenario and LboCell carries no direct tenant column (isolation is
    # transitive via scenario_id). Identical scoping to the prior implementation.
    cells = db.query(LboCell).filter(
        LboCell.scenario_id == scenario_id,
        LboCell.col_index == 1,
        LboCell.row_type == "input",
    ).all()
    rows = [
        {
            "section": c.section or "",
            "row_type": c.row_type or "input",
            "line_code": getattr(c, "line_code", None),
            "label": c.label or "",
            "driver_params": c.driver_params or {},
            "values": [float(c.value) if c.value is not None else None],
        }
        for c in cells
    ]
    return historical_bs_from_rows(rows)


def _cells_to_grid(cells) -> List[Dict[str, Any]]:
    """Convert flat cell list into row-oriented grid for the frontend."""
    rows_map: Dict[int, Dict[str, Any]] = {}
    for cell in cells:
        row_index = cell.row_index
        if row_index not in rows_map:
            rows_map[row_index] = {
                "row": row_index,
                "line_code": getattr(cell, "line_code", None),
                "label": "",
                "section": cell.section or "",
                "row_type": cell.row_type or "input",
                "display_fmt": cell.display_fmt or "number",
                "formula": cell.formula,
                "indent": cell.indent or 0,
                "driver_type": getattr(cell, "driver_type", "auto") or "auto",
                "driver_params": getattr(cell, "driver_params", {}) or {},
                "qbo_account_id": getattr(cell, "qbo_account_id", None),
                "values": [None] * NUM_YEAR_COLS,
                # Historical FY cells carry a NEGATIVE col_index (== seq: FY-1 → -1,
                # FY-2 → -2, FY-3 → -3) and are absent from the fixed-width positional
                # ``values`` array. Surface them here keyed by their (string) col_index
                # so the FE can render the historical columns it already builds from the
                # period spine (they rendered blank before this — the historicals were
                # ingested + stored but never serialized). Additive; ``values`` unchanged.
                "hist_values": {},
                # Post-close actuals band cells (Wave 2 / AC-CA-3) carry a HIGH col_index
                # (== seq >= BAND_COL_BASE: monthly at 100+, annual rollup at 1000+) and are
                # likewise absent from the positional ``values`` array. Surfaced in their OWN
                # additive map (kept disjoint from hist_values so no historical consumer picks
                # up the band); the FE resolves them by period_id + kind. Additive.
                "actual_values": {},
            }
        line_code = getattr(cell, "line_code", None)
        if rows_map[row_index]["line_code"] is None and line_code is not None:
            # Legacy/sparse rows may not carry identity on the first encountered
            # cell. Preserve the first exact persisted identity from any cell;
            # the canonical label cell below may refresh it when present.
            rows_map[row_index]["line_code"] = line_code
        if cell.col_index == 0:
            if line_code is not None:
                rows_map[row_index]["line_code"] = line_code
            rows_map[row_index]["label"] = cell.label or ""
            rows_map[row_index]["section"] = cell.section or ""
            rows_map[row_index]["row_type"] = cell.row_type or "input"
            rows_map[row_index]["display_fmt"] = cell.display_fmt or "number"
            rows_map[row_index]["indent"] = cell.indent or 0
            rows_map[row_index]["driver_type"] = getattr(cell, "driver_type", "auto") or "auto"
            rows_map[row_index]["driver_params"] = getattr(cell, "driver_params", {}) or {}
            if cell.formula:
                rows_map[row_index]["formula"] = cell.formula
        else:
            value_index = cell.col_index - 1
            if 0 <= value_index < NUM_YEAR_COLS:
                rows_map[row_index]["values"][value_index] = float(cell.value) if cell.value is not None else None
            elif cell.col_index <= -1:
                # Historical FY cell (negative col_index == seq) — keep it out of the
                # positional ``values`` array (which every legacy reader treats as 6-wide)
                # and surface it in the additive period-column map instead.
                rows_map[row_index]["hist_values"][str(cell.col_index)] = (
                    float(cell.value) if cell.value is not None else None
                )
            elif cell.col_index >= BAND_COL_BASE:
                # Post-close actuals band cell (high col_index == seq) — its OWN additive map,
                # disjoint from hist_values + the positional array (the off-positional firewall).
                rows_map[row_index]["actual_values"][str(cell.col_index)] = (
                    float(cell.value) if cell.value is not None else None
                )
            if cell.formula:
                rows_map[row_index]["formula"] = cell.formula

    rows = [rows_map[key] for key in sorted(rows_map.keys())]
    # AC-C3 reference column: surface the frozen pre-deal book anchor as a
    # top-level row field so the FE renders the pre-deal reference beside the
    # PF Y0 (additive presentation — the historical seed is never deleted).
    for row in rows:
        params = row.get("driver_params") or {}
        row["pre_deal_y0"] = params.get(PRE_DEAL_Y0_KEY) if isinstance(params, dict) else None  # SA-OK: dict.get on the in-memory row params, NOT an ORM query
    return rows


class PeriodMeta(TypedDict):
    """One serialized LBO period-axis column (plan 20260717-three-statement-spine
    C5-0 / AC-11): the typed shape the grid API additively carries so the FE
    renders + keys rows by ``period_id`` instead of by positional column index."""
    period_id: int
    seq: int
    kind: str
    label: str
    period_start: str | None
    period_end: str | None


def scenario_periods_meta(scenario_id: int, db: Session) -> list[PeriodMeta]:
    """The scenario's ``LboPeriod`` spine, ordered by ``seq``, serialized as the
    additive ``periods`` field on the grid payload (C5-0, AC-11).

    Reads the period rows scenario-scoped — the scenario is tenant-authorized
    UPSTREAM by the route's ``_require_lbo_scenario`` (``check_entity_tenant_access``
    on ``LboScenario``); this helper adds NO new auth path, mirroring the existing
    scenario-scoped ``_get_historical_bs`` read above and the ``deal.py`` LboPeriod
    read. A legacy pre-backfill scenario with no period rows returns ``[]`` — the FE
    degrades to its positional fallback; periods are NEVER fabricated. The ORM
    ``LboPeriod`` object never leaks: the six fields are serialized explicitly
    (calendar bounds as ISO strings or ``None`` — ``pf_open`` / forecast carry none).
    """
    periods = (
        db.query(LboPeriod)  # CROSS-TENANT-OK: scenario-scoped read; scenario_id is tenant-authorized upstream by the route's _require_lbo_scenario (check_entity_tenant_access on LboScenario) — identical scoping to deal.py's LboPeriod read and _get_historical_bs above
        .filter(LboPeriod.scenario_id == scenario_id)
        .order_by(LboPeriod.seq)
        .all()
    )
    return [
        {
            "period_id": p.id,
            "seq": p.seq,
            "kind": p.kind,
            "label": p.label,
            "period_start": p.period_start.isoformat() if p.period_start else None,
            "period_end": p.period_end.isoformat() if p.period_end else None,
        }
        for p in periods
    ]


def _load_grid_summary(scenario_id: int, db) -> Dict[str, Any]:
    """Return grid-level aggregates needed by S&U, debt schedule, and returns."""
    cells = (
        db.query(LboCell)
        .filter(LboCell.scenario_id == scenario_id)
        .order_by(LboCell.row_index, LboCell.col_index)
        .all()
    )
    row_map: Dict[int, Dict[str, Any]] = {}
    for cell in cells:
        if cell.row_index not in row_map:
            row_map[cell.row_index] = {
                "row": cell.row_index,
                "label": cell.label or "",
                "section": cell.section or "",
                "row_type": cell.row_type or "input",
                # PLAN-EXEMPT: AC-1 LBO v2-path remediation (Owen work order 2026-06-29). Carry
                # line_code so the summary keys off the STANDARDIZED slot, not an English label
                # absent from the v2 template (which left ebitda/fcf/revenue/net_income all 0).
                "line_code": cell.line_code,
                "values": [None] * NUM_YEAR_COLS,
            }
        if cell.col_index == 0 and cell.line_code:
            row_map[cell.row_index]["line_code"] = cell.line_code
        col_index = cell.col_index
        if 1 <= col_index <= NUM_YEAR_COLS:
            row_map[cell.row_index]["values"][col_index - 1] = cell.value

    grid = list(row_map.values())

    # PLAN-EXEMPT: AC-1 — match on line_code, NOT English label. ebitda/fcf are the rows AC-1
    # added to the template; net_income is the IS slot ("net_income"), deliberately distinct from
    # the cash-flow "cf_ni" row that carries the SAME human label "Net Income".
    by_code = {rd["line_code"]: rd for rd in grid if rd.get("line_code")}

    def _vec(code: str) -> list:
        rd = by_code.get(code)
        if not rd:
            return [0.0] * NUM_YEAR_COLS
        return [float(value or 0) for value in rd["values"]]

    ebitda_vals = _vec("ebitda")
    fcf_vals = _vec("fcf")
    revenue_vals = _vec("revenue")
    net_income_vals = _vec("net_income")

    return {
        "grid": grid,
        "ebitda": ebitda_vals,
        "fcf": fcf_vals,
        "revenue": revenue_vals,
        "net_income": net_income_vals,
    }


# ── AC-C3: the PF transaction-close bridge (grid Y0 = the PF opening BS) ──────


def stamp_pre_deal_anchor(row: dict) -> float:
    """Freeze (or read back) a transaction-set row's pre-deal book anchor.

    First bridge: the row's current Y0 IS the pre-deal book — stamp it into
    ``driver_params["pre_deal_y0"]``. Every later bridge: the Y0 value is
    already PF, so the STORED anchor is the book (never re-read the value —
    that is the corruption loop). Returns the anchor. Mutates ``driver_params``
    in place on the in-memory row; the router persists the stamp.
    """
    params = row.get("driver_params")
    if not isinstance(params, dict):
        params = {}
        row["driver_params"] = params
    anchor = params.get(PRE_DEAL_Y0_KEY)  # SA-OK: dict.get on an in-memory grid row's params, NOT an ORM query
    if anchor is None:
        vals = row.get("values") or []
        anchor = float(vals[0]) if vals and vals[0] is not None else 0.0
        params[PRE_DEAL_Y0_KEY] = anchor
    return float(anchor)


def pf_bridge_active(grid_rows: list[dict], su: dict) -> bool:
    """Whether the PF-close bridge applies to this grid + deal.

    Requires (1) a real transaction — enterprise value > 0 (a zero-EV scenario
    has no close to bridge; Y0 stays the pre-deal book), and (2) the standard
    3-statement skeleton (every ``_PF_SKELETON_CODES`` slot present) — the
    bridge never mangles a legacy/minimal grid it does not recognize.
    """
    uses = su.get("uses") or {}  # SA-OK: dict.get on the in-memory S&U result, NOT an ORM query
    if float(uses.get("enterprise_value") or 0) <= 0:  # SA-OK: dict.get on the in-memory S&U result, NOT an ORM query
        return False
    codes = {r.get("line_code") for r in grid_rows if r.get("line_code")}
    return _PF_SKELETON_CODES <= codes


def apply_pf_close_bridge(grid_rows: list[dict], su: dict) -> list[int]:
    """Rewrite the grid's Y0 equity + goodwill lines to the PF opening position.

    Consumes ``su["pf_opening_bs_asc805"]`` (the ASC-805 fee treatment — see
    sources_uses.py):

      * ``cash`` → the deal-basis PF opening cash (cash-free: the funded minimum
        operating cash, the seller keeps the book cash; equity purchase: the
        acquired book cash) — the same opening the engine projects from;
      * ``intangibles``  → pre-deal book intangibles + the ASC-805 goodwill plug
        (the template's "Intangibles & Goodwill" row carries transaction
        goodwill; the grid has no separate goodwill row, so the engine stamp
        folds the engine goodwill line into this one — DL-4);
      * ``common_equity`` → the contributed S&U equity plug (purchase accounting
        eliminates the target's book equity at close);
      * ``retained_earnings`` → −transaction_fees (costs EXPENSED at close per
        ASC 805; RE then rolls forward with projected NI from this base).

    Each rewritten row's pre-deal book is frozen as the ``pre_deal_y0`` anchor
    (the additive reference column; the historical seed is never deleted). A
    ``fixed`` forward driver on intangibles/common_equity is re-anchored to the
    PF level so projection carries the PF position, not the pre-deal book. The
    DEBT rows' PF Y0 (funded − OID per tranche) is assigned by the router's
    schedule-override pass, which owns the tranche→row mapping.

    Returns the bridged row indices (the router persists their anchor stamps).
    PURE over the in-memory rows — no DB.
    """
    by_code = {r.get("line_code"): r for r in grid_rows if r.get("line_code")}
    asc = su["pf_opening_bs_asc805"]
    bridged: list[int] = []

    def _set_y0(row: dict, value: float) -> None:
        vals = row.get("values")
        if not isinstance(vals, list) or not vals:
            row["values"] = vals = [None]
        vals[0] = round(float(value), 2)

    def _refit_fixed_driver(row: dict) -> None:
        # A `fixed` forward driver holds the row at params["amount"]; after the
        # bridge that level is the PF Y0, else Y1+ snaps back to the pre-deal book.
        if (row.get("driver_type") or "auto") == "fixed":
            params = row.get("driver_params")
            if isinstance(params, dict):
                vals = row.get("values") or []
                params["amount"] = float(vals[0]) if vals and vals[0] is not None else 0.0

    cash = by_code.get("cash")
    if cash is not None:
        stamp_pre_deal_anchor(cash)
        _set_y0(cash, float(asc["cash"]))
        bridged.append(int(cash["row"]))

    intangibles = by_code.get("intangibles")
    if intangibles is not None:
        book = stamp_pre_deal_anchor(intangibles)
        _set_y0(intangibles, book + float(asc["goodwill"]))
        _refit_fixed_driver(intangibles)
        bridged.append(int(intangibles["row"]))

    common_equity = by_code.get("common_equity")
    if common_equity is not None:
        stamp_pre_deal_anchor(common_equity)
        _set_y0(common_equity, float(asc["contributed_equity"]))
        _refit_fixed_driver(common_equity)
        bridged.append(int(common_equity["row"]))

    retained = by_code.get("retained_earnings")
    if retained is not None:
        stamp_pre_deal_anchor(retained)
        _set_y0(retained, float(asc["transaction_costs_expensed"]))
        bridged.append(int(retained["row"]))

    return bridged

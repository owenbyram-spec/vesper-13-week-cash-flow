"""Typed Pydantic v2 schema for the LBO ``driver_data`` assumptions (F-B4e Phase 3).

This is the **typed assumption spine** (Layer B): the single source of truth for the
LBO deal/operating assumptions that today live in an untyped ``driver_data`` JSON dict
and are consumed across the engines via scattered ``drivers.get("key", <inline default>)``.

The contract is **defaults-parity**: every field's default equals today's effective
default (the value ``_default_driver_data()`` produces, which is canonical when an
inline ``.get`` fallback diverges — see the plan §10 blocker log). A model built from
an empty or legacy ``driver_data`` therefore yields the SAME assumption values the
engines see today, so the projection / returns are byte-identical. Validation +
centralization are the only new behaviour; no number moves.

``model_config = ConfigDict(extra="allow")`` so an unknown legacy key in a stored
``driver_data`` is PRESERVED on round-trip (no silent data loss; forward-compatible
with the Phase-3b keys). Validators reject only the truly-impossible (negative
multiple, sweep% outside 0..1, negative hold/exit year) — never a currently-stored
sentinel value — so loading an existing scenario can never raise.

Pydantic v2 idiom only: ``ConfigDict`` / ``model_validate`` / ``model_dump`` /
``field_validator`` (+ ``@classmethod``). Never the v1 ``class Config`` / ``.dict()``.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator


class LboAssumptions(BaseModel):
    """The typed LBO assumption set. Defaults == today's ``_default_driver_data()`` values."""

    model_config = ConfigDict(extra="allow")

    # ── Operating / forecast drivers ─────────────────────────────────────────
    # revenue_growth_by_stream: per-stream growth-rate lists; the canonical default
    # carries the "overall" 5% × 5 ramp that _default_driver_data() ships.
    revenue_growth_by_stream: dict = Field(
        default_factory=lambda: {"overall": [0.05, 0.05, 0.05, 0.05, 0.05]}
    )
    margin_by_stream: dict = Field(default_factory=dict)
    base_revenue: float = 0
    ltm_ebitda: float = 0

    # ── Working-capital drivers ──────────────────────────────────────────────
    ar_dso: float = 30
    ap_dpo: float = 45
    other_ca_pct: float = 0.02
    accrued_pct: float = 0.05
    other_liab_pct: float = 0.02

    # ── Cost / capex / opex drivers ──────────────────────────────────────────
    capex_pct: float = 0.03
    direct_cost_pct: float = 0.05
    facilities_pct: float = 0.03
    other_ga_pct: float = 0.02

    # ── Goodwill (book-vs-tax split — RD-1 row 17; plan 20260706 AC-2A.1) ────
    # BOOK (ASC 350-20, binding): impairment-only by default — 0 means NO book
    # amortization; a positive value is the private-company election (≤ 10 years).
    # TAX (IRC §197, binding): 15-year ratable — the labeled cash-tax proxy
    # schedule, never the book P&L line. The retired `goodwill_amort_years` key
    # WAS the §197 tax life mislabeled as book; a stored legacy value maps to the
    # tax life at the engine boundary (engine_support.resolve_goodwill_lives).
    goodwill_amount: float = 0
    goodwill_book_amort_years: int = 0
    goodwill_tax_amort_years: int = 15

    # ── Tax ──────────────────────────────────────────────────────────────────
    tax_rate: float = 0.21

    # ── Deal / returns assumptions ───────────────────────────────────────────
    # entry/exit multiples: canonical 8.0 (the _default_driver_data value). The
    # divergent inline `.get(..., 0)` fallbacks at sources_uses:27 / exit_waterfall:67
    # were dead code (the key is always present) — see plan §10.
    entry_ebitda_multiple: float = 8.0
    exit_ebitda_multiple: float = 8.0
    # exit_year: canonical static 4 (the _default_driver_data value); the dynamic
    # `len(forecast_years)-1` fallback at exit_waterfall:72 was reachable only when
    # the key was absent, which it never is post-default — see plan §10.
    exit_year: int = 4
    transaction_fees_pct: float = 0.03
    equity_carry_pct: float = 0.20

    # distribution_pct_of_fcf: consumed at forecast_engine:262 (default 0.0) but
    # absent from _default_driver_data(); added here at its consumed default so the
    # spine emits the same number the engine reads today — see plan §10.
    distribution_pct_of_fcf: float = 0.0

    # ── Cash-sweep realism (RD-1 row 5; plan 20260706 AC-2A.1) ───────────────
    # ALL per-deal credit-agreement inputs — NO market grid is encoded (gap G1:
    # the folklore 50/25/0 step-down grid was not groundable to a primary source;
    # refuse over fabricate). Defaults are parity with the pre-existing engine:
    # flat 100% sweep of the current year's excess, no minimum-cash floor.
    sweep_pct: float = 1.0
    sweep_pct_by_year: list = Field(default_factory=list)
    # Minimum operating cash the sweep/distributions never dip below. A sponsor
    # input here; a QBO runway-based anchor derivation is a future extension —
    # no anchor is fabricated for it (assumption_proposals WS-5).
    min_cash_floor: float = 0.0

    # ── Management rollover (RD-1 row 15 build-new) ──────────────────────────
    # Dollar amount of seller/management proceeds rolled into the new entity's
    # equity: a SOURCE that reduces sponsor cash equity (never a Use), tracked as
    # a second equity class through the exit waterfall. Sponsor input, no QBO anchor.
    management_rollover_amount: float = 0.0

    # ── Deal basis (sources_uses.resolve_deal_basis) ─────────────────────────
    # "cash_free_debt_free" (default): EV prices the business free of cash and
    # debt — the seller keeps the target's book cash, and the PF opening cash is
    # the explicit minimum_operating_cash the sponsor funds as a Use.
    # "equity_purchase": equity = EV + book cash − book debt, so the acquired
    # cash is paid for (a Use) and stays on the PF opening balance sheet.
    deal_basis: str = "cash_free_debt_free"
    minimum_operating_cash: float = 0.0

    # ── Exit EBITDA basis (RD-1 row 16 consumer wiring) ──────────────────────
    # "book" (parity default) or "adjusted" — whether exit EV prices off book
    # EBITDA or adjusted EBITDA (the documented add-back set). An explicit driver
    # so valuation-vs-book is a stated choice, never an accident.
    exit_ebitda_basis: str = "book"

    # ── Engine market mechanics (AC-C4, plan 20260716-five-domain-remediation) ──
    # §197 tax-goodwill election: when True the engine deducts the labeled tax
    # amortization from CASH taxes only (book D&A untouched — capex_depreciation.py
    # feed into waterfall_solve's cash-tax base). Default OFF — parity with the
    # engine's drivers.get("section_197_election", False).
    section_197_election: bool = False
    # Optional preferred-return carry hurdle: an annual rate compounded over the
    # hold (exit_waterfall.py — hard hurdle, no catch-up). 0.0 = no hurdle —
    # parity with the engine's drivers.get("preferred_return_pct", 0.0).
    preferred_return_pct: float = 0.0

    # ── Free-form adjustment map ─────────────────────────────────────────────
    # Per add-back: a legacy [v0, v1, ...] list, or the structured
    # {"values": [...], "provenance": {...}} record (per-item provenance — the
    # quality-of-earnings discipline, RD-1 row 16).
    ebitda_adjustments: dict = Field(default_factory=dict)

    @field_validator("entry_ebitda_multiple", "exit_ebitda_multiple")
    @classmethod
    def _non_negative_multiple(cls, v: float) -> float:
        """A negative EBITDA multiple is impossible. (Zero is tolerated — a legacy sentinel.)"""
        if v < 0:
            raise ValueError("EBITDA multiple cannot be negative")
        return v

    @field_validator("equity_carry_pct")
    @classmethod
    def _sweep_in_unit_interval(cls, v: float) -> float:
        """A carry / sweep percentage outside 0..1 is impossible (it is a fraction)."""
        if v < 0 or v > 1:
            raise ValueError("equity_carry_pct must be within 0..1")
        return v

    @field_validator("exit_year", "goodwill_tax_amort_years")
    @classmethod
    def _non_negative_year(cls, v: int) -> int:
        """A negative year/term is impossible."""
        if v < 0:
            raise ValueError("year/term cannot be negative")
        return v

    @field_validator("goodwill_book_amort_years")
    @classmethod
    def _book_goodwill_election_bounds(cls, v: int) -> int:
        """ASC 350-20: book goodwill is impairment-only (0) or the private-company
        election — straight-line over AT MOST 10 years. 11+ is not a valid book
        election (15 is the IRC §197 TAX life; use goodwill_tax_amort_years)."""
        if v < 0:
            raise ValueError("goodwill_book_amort_years cannot be negative")
        if v > 10:
            raise ValueError(
                "goodwill_book_amort_years must be ≤ 10 (ASC 350-20 private-company "
                "election); 15-year ratable amortization is the IRC §197 TAX life — "
                "set goodwill_tax_amort_years instead"
            )
        return v

    @field_validator("sweep_pct")
    @classmethod
    def _sweep_pct_in_unit_interval(cls, v: float) -> float:
        """A sweep percentage outside 0..1 is impossible (it is a fraction)."""
        if v < 0 or v > 1:
            raise ValueError("sweep_pct must be within 0..1")
        return v

    @field_validator("distribution_pct_of_fcf")
    @classmethod
    def _distribution_pct_in_unit_interval(cls, v: float) -> float:
        """A distribution percentage outside 0..1 is impossible (it is a FRACTION of
        post-sweep FCF — "50" would distribute 50× FCF into IRR/TVPI, the AC-FRR-11a
        refuter gap). The deal-drivers PATCH rejects out-of-range at WRITE time, so
        stored driver_data can never carry a value that trips this validator (which
        would otherwise hit the load-time defaults-fallback below)."""
        if v < 0 or v > 1:
            raise ValueError("distribution_pct_of_fcf must be within 0..1")
        return v

    @field_validator("sweep_pct_by_year")
    @classmethod
    def _sweep_stepdowns_in_unit_interval(cls, v: list) -> list:
        """Every per-year sweep step-down is a fraction within 0..1."""
        for item in v:
            val = float(item)
            if val < 0 or val > 1:
                raise ValueError("each sweep_pct_by_year entry must be within 0..1")
        return v

    @field_validator("preferred_return_pct")
    @classmethod
    def _non_negative_pref(cls, v: float) -> float:
        """A negative preferred-return hurdle is impossible (0.0 = no hurdle)."""
        if v < 0:
            raise ValueError("preferred_return_pct cannot be negative")
        return v

    @field_validator("min_cash_floor", "management_rollover_amount", "minimum_operating_cash")
    @classmethod
    def _non_negative_amount(cls, v: float) -> float:
        """A negative cash floor / rollover / operating-cash amount is impossible."""
        if v < 0:
            raise ValueError("amount cannot be negative")
        return v

    @field_validator("deal_basis", "minimum_operating_cash", mode="before")
    @classmethod
    def _unset_deal_basis_is_the_default(cls, v: object, info: ValidationInfo) -> object:
        """A stored null on the deal-basis keys means "never set" — the value every
        scenario created before these keys existed round-trips through GET/PATCH
        /deal-drivers. It resolves to the named default (as sources_uses does)
        instead of failing validation, which would discard the whole assumption set."""
        if v is None:
            return cls.model_fields[info.field_name].default
        return v

    @field_validator("deal_basis")
    @classmethod
    def _deal_basis_is_known(cls, v: str) -> str:
        """The target's cash is priced on exactly one stated basis."""
        if v not in ("cash_free_debt_free", "equity_purchase"):
            raise ValueError('deal_basis must be "cash_free_debt_free" or "equity_purchase"')
        return v

    @field_validator("exit_ebitda_basis")
    @classmethod
    def _exit_basis_is_known(cls, v: str) -> str:
        """Exit EV prices off exactly one stated basis: book or adjusted EBITDA."""
        if v not in ("book", "adjusted"):
            raise ValueError('exit_ebitda_basis must be "book" or "adjusted"')
        return v


def default_assumptions() -> dict:
    """The canonical default assumption dict — the one source of truth.

    ``_default_driver_data()`` (app/lbo/serializers.py) delegates here so the typed
    model's defaults are the single definition of "a new scenario's assumptions."
    """
    return LboAssumptions().model_dump()


def validate_assumptions(stored: dict | None) -> dict:
    """Validate + default a stored ``driver_data`` dict at the load/save boundary.

    Returns a validated, fully-defaulted dict the engines can consume. Legacy-tolerant
    (WS-2): on ``ValidationError`` (a stored value the lenient validators reject) this
    falls back to the defaults + a redacted log, NEVER a 500 mid-load. ``extra="allow"``
    means unknown stored keys are preserved on the round-trip (WS-3).
    """
    try:
        return LboAssumptions.model_validate(stored or {}).model_dump()
    except Exception as e:  # noqa: BLE001 — legacy-tolerant boundary; never 500 a load (WS-2)
        from ..services.redaction import redact_to_string

        import logging

        logging.getLogger(__name__).warning(
            "lbo driver_data failed assumption validation; falling back to defaults: %s",
            redact_to_string(e),
        )
        return LboAssumptions().model_dump()

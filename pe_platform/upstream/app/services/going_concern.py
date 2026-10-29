"""app/services/going_concern.py — the deterministic ASC 205-40 going-concern
INDICATOR engine (Wave 3.7, RK-6).

Two clean seams (plan AC-1, E5):

1. ``evaluate(signals) -> IndicatorSet`` — a PURE function (0 DB, 0 IO, 0 writes):
   it applies the binding ASC 205-40-55-2 indicator thresholds to an INJECTED
   ``GoingConcernSignals`` struct. This is where the engine owns every number and
   where the hand-computed goldens live.

2. ``gather_signals(db, company_id, qbo_client=...)`` — DB-gather adapters that
   READ Vesper Bot's already-computed substrate (covenant verdict, ratio /
   working-capital / CFO analytics, the 13-week runway) into a
   ``GoingConcernSignals``. Every substrate read honest-degrades to ``None``
   (→ ``indeterminate``) on an absent input — never a fabricated number.

THE REFUSE-OVER-FABRICATE BOUNDARY (plan AC-3 / SYNTHESIS §4): ASC 205-40
substantial-doubt determination + management's-plans mitigation are JUDGMENTS
reserved for management and the CPA (205-40-50-1 / -50-4 / -50-7). This engine
surfaces the deterministic INDICATORS (205-40-55-2) and a triage severity, and
carries ``requires_cpa_review=True`` PERMANENTLY. It NEVER concludes going
concern, never emits a verdict, a plans assessment, a disclosure draft, a
probability, a fabricated all-clear, or a fabricated alarm. Provenance cites
``205-40-55-2`` (indicators) + ``205-40-50-1`` (period) — NEVER ``-25-X`` (that
numbering does not exist in the Codification).
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..qbo_client import QBOClient

logger = logging.getLogger(__name__)


# ── Thresholds + citations (the engine owns every number) ─────────────────────

# GC-LIQ-1: the ASC 205-40-50-1 one-year look-forward, in weeks. A projected
# runway shorter than this fires the liquidity indicator. Exactly 52 wk does NOT
# fire — a runway that covers the full window is not a shortfall.
GC_LIQ_LOOK_FORWARD_WEEKS = 52
GC_LIQ_LOOK_FORWARD_MONTHS = 12
# GC-NFT-1: "recurring" operating losses = at least this many CONSECUTIVE loss
# periods (one loss period is not "recurring"; the aggregate is the human's).
RECURRING_LOSS_MIN_PERIODS = 2
# GC-NFT-4: a current ratio strictly below 1.0 is an adverse key ratio.
CURRENT_RATIO_ADVERSE_THRESHOLD = 1.0
# GC-LIQ-1 honesty stamp: no true 12-month cash engine exists on disk; v1 backs
# the one-year runway with the predict_13_week_cash constant-burn extrapolation
# (SYNTHESIS §5.a). Stamped so the caveat is never silently faked as a 12-month
# number.
GC_LIQ_EXTRAPOLATION_BASIS = "constant_burn_beyond_13wk"
# The assessment anchor is the engine run date, stamped as a PROXY for the ASC
# 205-40-50-1 issuance date (a QBO SMB issues no GAAP-complete statements).
ASSESSMENT_ANCHOR_BASIS = "evaluation_run_date_proxy"

INDICATORS_CITATION = "ASC 205-40-55-2"
LOOK_FORWARD_CITATION = "ASC 205-40-50-1"

# The fixed indicators-only notice (no determination). Carried on the engine
# output AND the escalation alert so a downstream narrator inherits the boundary.
GOING_CONCERN_NOTICE = (
    "Going-concern INDICATORS only — no substantial-doubt determination. "
    "ASC 205-40 evaluation of the conditions in the aggregate and of management's "
    "plans is reserved for management and the CPA (205-40-50-1 / -50-4 / -50-7)."
)

# 205-40-55-2(c)/(d) + the qualitative members of (b): DECLARED, never silently
# dropped, so a CPA sees the deterministic pass covered only the computable subset
# of an in-the-aggregate standard. Described FUNCTIONALLY (E3) — these are NOT a
# claimed-verbatim reproduction of the FASB category header text.
NOT_EVALUATED_CATEGORIES = (
    "205-40-55-2(b) — qualitative 'other indications of financial difficulty' "
    "(dividend arrearages, denial of usual trade credit, debt-restructuring need, "
    "statutory-capital noncompliance, need to raise capital or dispose of "
    "substantial assets): not deterministically detectable from the ledger.",
    "205-40-55-2(c) — internal matters (work stoppages, single-project dependence, "
    "uneconomic long-term commitments, need to revise operations): qualitative, "
    "outside deterministic scope.",
    "205-40-55-2(d) — external matters (legal proceedings or legislation, loss of a "
    "key franchise/license/patent/customer/supplier, uninsured catastrophe): "
    "qualitative/exogenous, outside deterministic scope.",
)

# GC-FIN-1 + GC-LIQ-1 are the strongest 205-40-55-2 substrate (an actual loan
# default; already-out-of-runway liquidity distress). Their presence rolls the
# triage severity to "high"; so does ANY 3-indicator aggregate. This is a FACTUAL
# roll-up of which indicators fired — NEVER a probability or a doubt likelihood.
_SEVERE_INDICATOR_IDS = frozenset({"GC-FIN-1", "GC-LIQ-1"})
_SEVERITY_TO_WATCHDOG = {"elevated": "warning", "high": "critical"}


# ── The output-contract data structures ──────────────────────────────────────

@dataclass(frozen=True)
class GoingConcernSignals:
    """The INJECTED signal struct ``evaluate`` reasons over — data in, data out.

    ``runway_forecast_available`` disambiguates ``runway_weeks is None`` (E2): the
    13-week forecast returns ``runway_weeks=None`` for BOTH a healthy no-burn
    tenant AND an already-insolvent one, so the presence of a computed forecast
    plus ``current_cash`` tells them apart. ``False`` means the forecast could not
    be computed at all → GC-LIQ-1 is ``indeterminate``, never a fabricated
    all-clear.
    """

    current_assets: float | None = None
    current_liabilities: float | None = None
    current_ratio: float | None = None
    equity: float | None = None
    operating_income_series: Sequence[float] | None = None
    cfo: float | None = None
    covenant_status: str | None = None  # PASS | WARN | BREACH | UNKNOWN | None
    runway_forecast_available: bool = False
    runway_weeks: int | None = None
    current_cash: float | None = None
    lowest_balance: float | None = None
    negative_cash_weeks: Sequence[int] | None = None


@dataclass(frozen=True)
class IndicatorHit:
    """One fired binding 205-40-55-2 condition: the category, the value + threshold
    that fired it, and the source signal. A hit is a SIGNAL, never a conclusion."""

    indicator_id: str
    asc_category: str
    category_label: str
    basis: str
    source: str
    value: float | None = None
    threshold: float | None = None
    extrapolation_basis: str | None = None

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "indicator_id": self.indicator_id,
            "asc_category": self.asc_category,
            "category_label": self.category_label,
            "basis": self.basis,
            "source": self.source,
            "value": self.value,
            "threshold": self.threshold,
        }
        if self.extrapolation_basis:
            payload["extrapolation_basis"] = self.extrapolation_basis
        return payload


@dataclass(frozen=True)
class IndicatorSet:
    """The escalate-only output contract (SYNTHESIS §4.1). Deliberately carries NO
    ``conclusion`` / ``verdict`` / ``determination`` field — those are the human's
    205-40-50-4 judgment (plan AC-3)."""

    indicators: list[IndicatorHit]
    not_evaluated: list[str]
    indeterminate: list[dict[str, str]]
    severity: str  # none | elevated | high — a triage roll-up, NEVER a probability
    requires_cpa_review: bool
    notice: str
    assessment_anchor_date: str
    assessment_anchor_basis: str
    look_forward: dict[str, object]
    provenance: dict[str, object]

    @property
    def fired_indicator_ids(self) -> list[str]:
        return [hit.indicator_id for hit in self.indicators]

    def to_dict(self) -> dict[str, object]:
        return {
            "indicators": [hit.to_dict() for hit in self.indicators],
            "not_evaluated": list(self.not_evaluated),
            "indeterminate": list(self.indeterminate),
            "severity": self.severity,
            "requires_cpa_review": self.requires_cpa_review,
            "notice": self.notice,
            "assessment_anchor_date": self.assessment_anchor_date,
            "assessment_anchor_basis": self.assessment_anchor_basis,
            "look_forward": dict(self.look_forward),
            "provenance": dict(self.provenance),
        }


# ── The PURE engine (0 DB, 0 IO, 0 writes) ────────────────────────────────────

def evaluate(signals: GoingConcernSignals, *, anchor_date: date | None = None) -> IndicatorSet:
    """Apply the binding ASC 205-40-55-2 indicator thresholds to injected signals.

    Returns the escalate-only contract. Fires only on positive evidence; declares
    every un-computable indicator as ``indeterminate`` (missing input, NOT a
    negative conclusion); and NEVER emits a going-concern conclusion.
    """
    anchor = anchor_date or date.today()
    fired: list[IndicatorHit] = []
    indeterminate: list[dict[str, str]] = []

    _eval_recurring_losses(signals, fired, indeterminate)
    _eval_working_capital(signals, fired, indeterminate)
    _eval_negative_cfo(signals, fired, indeterminate)
    _eval_adverse_ratio(signals, fired, indeterminate)
    _eval_covenant(signals, fired, indeterminate)
    _eval_runway(signals, fired, indeterminate)

    return IndicatorSet(
        indicators=fired,
        not_evaluated=list(NOT_EVALUATED_CATEGORIES),
        indeterminate=indeterminate,
        severity=_roll_up_severity(fired),
        requires_cpa_review=True,  # AC-3: never lifts
        notice=GOING_CONCERN_NOTICE,
        assessment_anchor_date=anchor.isoformat(),
        assessment_anchor_basis=ASSESSMENT_ANCHOR_BASIS,
        look_forward=_look_forward(anchor),
        provenance=_provenance(),
    )


def _eval_recurring_losses(s: GoingConcernSignals, fired: list, indeterminate: list) -> None:
    """GC-NFT-1 — recurring operating losses (55-2(a))."""
    series = list(s.operating_income_series) if s.operating_income_series is not None else None
    if series is None or len(series) < RECURRING_LOSS_MIN_PERIODS:
        indeterminate.append({
            "indicator": "GC-NFT-1",
            "reason": f"operating-income history for <{RECURRING_LOSS_MIN_PERIODS} consecutive periods is unavailable",
        })
        return
    recent = series[-RECURRING_LOSS_MIN_PERIODS:]
    if all(v < 0 for v in recent):
        fired.append(IndicatorHit(
            indicator_id="GC-NFT-1", asc_category="205-40-55-2(a)",
            category_label="negative financial trend — recurring operating losses",
            basis=f"operating income negative across the last {RECURRING_LOSS_MIN_PERIODS} periods ({recent})",
            source="ratios.compute_period_pl_kpis (operating income = gross_profit − operating_expenses)",
            value=float(recent[-1]), threshold=0.0,
        ))


def _eval_working_capital(s: GoingConcernSignals, fired: list, indeterminate: list) -> None:
    """GC-NFT-2 — working-capital deficiency (55-2(a))."""
    if s.current_assets is None or s.current_liabilities is None:
        indeterminate.append({"indicator": "GC-NFT-2", "reason": "current assets and/or current liabilities unavailable"})
        return
    working_capital = s.current_assets - s.current_liabilities
    if working_capital < 0:
        fired.append(IndicatorHit(
            indicator_id="GC-NFT-2", asc_category="205-40-55-2(a)",
            category_label="negative financial trend — working-capital deficiency",
            basis=f"current assets {s.current_assets} − current liabilities {s.current_liabilities} = {working_capital} < 0",
            source="ratios.compute_balance_sheet_kpis",
            value=float(working_capital), threshold=0.0,
        ))


def _eval_negative_cfo(s: GoingConcernSignals, fired: list, indeterminate: list) -> None:
    """GC-NFT-3 — negative operating cash flow (55-2(a))."""
    if s.cfo is None:
        indeterminate.append({"indicator": "GC-NFT-3", "reason": "operating cash flow (CFO) unavailable"})
        return
    if s.cfo < 0:
        fired.append(IndicatorHit(
            indicator_id="GC-NFT-3", asc_category="205-40-55-2(a)",
            category_label="negative financial trend — negative operating cash flow",
            basis=f"operating cash flow {s.cfo} < 0",
            source="ratios.compute_cashflow_kpis",
            value=float(s.cfo), threshold=0.0,
        ))


def _eval_adverse_ratio(s: GoingConcernSignals, fired: list, indeterminate: list) -> None:
    """GC-NFT-4 — adverse key ratio: current ratio < 1.0 OR negative equity (55-2(a)).

    An OR of two independently-computable legs. Fire if EITHER KNOWN leg is adverse. Otherwise,
    if EITHER leg is unavailable the OR is genuinely undeterminable — the missing leg could still
    be adverse — so it degrades to ``indeterminate``, NEVER a silent partial all-clear (the
    adversarial-verify catch: a clean known leg + a None leg must not read as "assessed and passed").
    """
    ratio_adverse = s.current_ratio is not None and s.current_ratio < CURRENT_RATIO_ADVERSE_THRESHOLD
    equity_adverse = s.equity is not None and s.equity < 0
    if ratio_adverse or equity_adverse:
        legs = []
        if ratio_adverse:
            legs.append(f"current ratio {s.current_ratio} < {CURRENT_RATIO_ADVERSE_THRESHOLD}")
        if equity_adverse:
            legs.append(f"equity {s.equity} < 0 (negative net worth)")
        fired.append(IndicatorHit(
            indicator_id="GC-NFT-4", asc_category="205-40-55-2(a)",
            category_label="negative financial trend — adverse key ratio",
            basis="; ".join(legs),
            source="ratios.compute_balance_sheet_kpis / compute_ratios",
            value=float(s.current_ratio) if s.current_ratio is not None else None,
            threshold=CURRENT_RATIO_ADVERSE_THRESHOLD,
        ))
        return
    if s.current_ratio is None or s.equity is None:
        indeterminate.append({
            "indicator": "GC-NFT-4",
            "reason": "adverse-ratio undeterminable — a leg (current ratio and/or equity) is unavailable and no known leg is adverse",
        })


def _eval_covenant(s: GoingConcernSignals, fired: list, indeterminate: list) -> None:
    """GC-FIN-1 — loan default / covenant breach (55-2(b)). Only BREACH fires;
    WARN is near-breach, not a default; UNKNOWN / no-covenants → indeterminate."""
    status = s.covenant_status
    if status == "BREACH":
        fired.append(IndicatorHit(
            indicator_id="GC-FIN-1", asc_category="205-40-55-2(b)",
            category_label="other indication of financial difficulty — covenant breach / loan default",
            basis="at least one active debt covenant is in BREACH",
            source="covenants._compute_status / latest CovenantRecord.status",
        ))
    elif status is None or status == "UNKNOWN":
        reason = "no active debt covenants on file" if status is None else "covenant value unavailable this period (UNKNOWN)"
        indeterminate.append({"indicator": "GC-FIN-1", "reason": reason})


def _eval_runway(s: GoingConcernSignals, fired: list, indeterminate: list) -> None:
    """GC-LIQ-1 — runway below the one-year look-forward (55-2(a)/(b)).

    E2 None-disambiguation + the projected-trough guard (adversarial-verify catches). A missing
    forecast is ``indeterminate``. A finite runway below 52 wk fires. Already-insolvent
    (``current_cash <= 0``) fires. A forecast that projects the balance BELOW ZERO within the
    13-week window fires (a real liquidity event even when the point runway is None/long). And a
    ``runway_weeks is None`` with ``current_cash`` UNKNOWN is ``indeterminate`` — cannot
    disambiguate insolvency from no-burn — NEVER a silent no-fire. Only a genuinely-healthy
    (runway ≥ 52 wk, or None with positive cash and no negative trough) case does not fire.
    """
    if not s.runway_forecast_available:
        indeterminate.append({"indicator": "GC-LIQ-1", "reason": "13-week cash forecast could not be computed (input unavailable)"})
        return
    trough_negative = (s.lowest_balance is not None and s.lowest_balance < 0) or bool(s.negative_cash_weeks)
    # (1) point runway estimate below the one-year look-forward.
    if s.runway_weeks is not None and s.runway_weeks < GC_LIQ_LOOK_FORWARD_WEEKS:
        fired.append(IndicatorHit(
            indicator_id="GC-LIQ-1", asc_category="205-40-55-2(a)",
            category_label="negative financial trend — projected cash runway below the one-year look-forward",
            basis=f"projected runway {s.runway_weeks} wk < the {GC_LIQ_LOOK_FORWARD_WEEKS}-week (12-month) look-forward",
            source="predictive_cash.predict_13_week_cash",
            value=float(s.runway_weeks), threshold=float(GC_LIQ_LOOK_FORWARD_WEEKS),
            extrapolation_basis=GC_LIQ_EXTRAPOLATION_BASIS,
        ))
        return
    # (2) already insolvent — the runway_weeks=None current_cash<=0 case.
    if s.current_cash is not None and s.current_cash <= 0:
        fired.append(IndicatorHit(
            indicator_id="GC-LIQ-1", asc_category="205-40-55-2(a)",
            category_label="negative financial trend — insolvency (current cash at or below zero)",
            basis=f"current cash {s.current_cash} <= 0 (already insolvent; no positive runway to project)",
            source="predictive_cash.predict_13_week_cash (starting_cash)",
            value=float(s.current_cash), threshold=0.0,
            extrapolation_basis=GC_LIQ_EXTRAPOLATION_BASIS,
        ))
        return
    # (3) the forecast projects the balance below zero WITHIN the window — a real liquidity event
    #     even when the point runway is None (net cash-positive on average) or ≥ 52 wk.
    if trough_negative:
        fired.append(IndicatorHit(
            indicator_id="GC-LIQ-1", asc_category="205-40-55-2(a)",
            category_label="negative financial trend — projected cash balance below zero within the forecast window",
            basis=f"forecast trough {s.lowest_balance} < 0 / negative-balance weeks {list(s.negative_cash_weeks or [])} within the 13-week window",
            source="predictive_cash.predict_13_week_cash (summary)",
            value=float(s.lowest_balance) if s.lowest_balance is not None else None, threshold=0.0,
            extrapolation_basis=GC_LIQ_EXTRAPOLATION_BASIS,
        ))
        return
    # (4) runway un-computable AND current cash unknown → cannot disambiguate → indeterminate,
    #     never a silent no-fire (the disambiguator itself is absent).
    if s.runway_weeks is None and s.current_cash is None:
        indeterminate.append({"indicator": "GC-LIQ-1", "reason": "runway un-computable and current cash unavailable — cannot disambiguate insolvency from no-burn"})
        return
    # else: runway_weeks ≥ 52 (point estimate covers the window) OR None with positive cash and no
    #       projected negative trough → genuinely healthy → not fired.


def _roll_up_severity(fired: list[IndicatorHit]) -> str:
    """Factual triage roll-up (NEVER a probability). ``none`` when nothing fired —
    which is NOT an all-clear; ``high`` on a severe indicator or a 3-indicator
    aggregate; ``elevated`` otherwise."""
    if not fired:
        return "none"
    ids = {hit.indicator_id for hit in fired}
    if ids & _SEVERE_INDICATOR_IDS or len(fired) >= 3:
        return "high"
    return "elevated"


def severity_to_watchdog_severity(severity: str) -> str:
    """Map the engine ordinal to the WatchdogAlert domain (elevated→warning,
    high→critical). ``none`` is intentionally absent — the runner never alerts on
    it (a KeyError would be a caller bug)."""
    return _SEVERITY_TO_WATCHDOG[severity]


def _look_forward(anchor: date) -> dict[str, object]:
    return {
        "start": anchor.isoformat(),
        "end": _plus_one_year(anchor).isoformat(),
        "window_months": GC_LIQ_LOOK_FORWARD_MONTHS,
        "window_weeks": GC_LIQ_LOOK_FORWARD_WEEKS,
    }


def _plus_one_year(anchor: date) -> date:
    try:
        return anchor.replace(year=anchor.year + 1)
    except ValueError:
        # Feb 29 → Feb 28 the following (non-leap) year.
        return anchor.replace(year=anchor.year + 1, day=28)


def _provenance() -> dict[str, object]:
    return {
        "indicators_citation": INDICATORS_CITATION,
        "look_forward_citation": LOOK_FORWARD_CITATION,
        "standard": "ASU 2014-15 (Subtopic 205-40) — Presentation of Financial Statements — Going Concern",
        "binding_status": "binding",
        "citation_granularity": (
            "indicators cited at the paragraph level (205-40-55-2); the four categories are "
            "described functionally, not reproduced as verbatim standard header text"
        ),
    }


# ── DB-gather adapters (read the substrate; honest-degrade; NEVER fabricate) ───

def gather_signals(db, company_id: str, *, qbo_client: QBOClient | None = None) -> GoingConcernSignals:
    """Read Vesper Bot's existing substrate into a ``GoingConcernSignals``.

    The covenant leg is pure-DB (the strongest, no-new-math signal). The
    ratio/CFO/runway legs need a live QBO client; without one — or on any read
    failure — the corresponding signal stays ``None`` so ``evaluate`` degrades it
    to ``indeterminate`` (never a fabricated all-clear).
    """
    covenant_status = _latest_covenant_status(db, company_id)

    balance_sheet = operating_income_series = cfo = None
    runway: dict[str, object] = {}
    if qbo_client is not None:
        balance_sheet = _safe("balance_sheet", _read_balance_sheet, qbo_client)
        operating_income_series = _safe("operating_income_series", _read_operating_income_series, qbo_client, db, company_id)
        cfo = _safe("cashflow", _read_cfo, qbo_client, db, company_id)
        runway = _safe("runway", _read_runway, qbo_client, db, company_id) or {}

    current_assets = balance_sheet.get("current_assets") if balance_sheet else None
    current_liabilities = balance_sheet.get("current_liabilities") if balance_sheet else None
    equity = balance_sheet.get("total_equity") if balance_sheet else None
    current_ratio = None
    if current_assets is not None and current_liabilities:  # non-zero denominator only
        current_ratio = current_assets / current_liabilities

    return GoingConcernSignals(
        current_assets=current_assets,
        current_liabilities=current_liabilities,
        current_ratio=current_ratio,
        equity=equity,
        operating_income_series=operating_income_series,
        cfo=cfo,
        covenant_status=covenant_status,
        runway_forecast_available=bool(runway),
        runway_weeks=runway.get("runway_weeks"),
        current_cash=runway.get("current_cash"),
        lowest_balance=runway.get("lowest_balance"),
        negative_cash_weeks=runway.get("negative_cash_weeks"),
    )


def _latest_covenant_status(db, company_id: str) -> str | None:
    """The worst latest stored verdict across the company's active covenants.

    Reads the latest ``CovenantRecord.status`` per active ``DebtCovenant`` (2
    queries, no N+1) and returns the dominant verdict (BREACH > UNKNOWN > WARN >
    PASS). ``None`` when the company tracks no active covenants; ``UNKNOWN`` when
    covenants exist but have no snapshot yet — never a fabricated PASS.
    """
    from ..models import CovenantRecord, DebtCovenant

    covenant_ids = [
        row[0]
        for row in db.query(DebtCovenant.id)
        .filter(DebtCovenant.company_id == company_id, DebtCovenant.active == True)  # noqa: E712
        .all()
    ]
    if not covenant_ids:
        return None

    rows = (
        db.query(CovenantRecord.covenant_id, CovenantRecord.period, CovenantRecord.status)
        .filter(CovenantRecord.company_id == company_id, CovenantRecord.covenant_id.in_(covenant_ids))
        .all()
    )
    latest: dict[int, tuple[str, str]] = {}  # covenant_id → (period, status)
    for covenant_id, period, status in rows:
        best_period = latest[covenant_id][0] if covenant_id in latest else None
        if best_period is None or period > best_period:
            latest[covenant_id] = (period, status)
    statuses = [status for _, status in latest.values()]
    if not statuses:
        return "UNKNOWN"
    for rank in ("BREACH", "UNKNOWN", "WARN", "PASS"):
        if rank in statuses:
            return rank
    return "UNKNOWN"


def _safe(label: str, fn: Callable[..., object], *args: object, **kwargs: object) -> object | None:
    from .redaction import redact_to_string

    try:
        return fn(*args, **kwargs)
    except Exception as exc:  # CRAFT-OK: honest-degrade any substrate read to None (→ indeterminate), never a fabricated number
        logger.warning("[going_concern] %s signal unavailable: %s", label, redact_to_string(exc))
        return None


def _read_balance_sheet(qbo_client: QBOClient) -> dict[str, float]:
    from ..analysis.ratios import compute_balance_sheet_kpis

    return compute_balance_sheet_kpis(qbo_client, date.today().isoformat())


def _read_cfo(qbo_client: QBOClient, db, company_id: str) -> float:
    from ..analysis.ratios import compute_cashflow_kpis, resolve_period

    period = resolve_period(None, None, db=db, realm_id=company_id)
    return compute_cashflow_kpis(qbo_client, period)["cfo"]


def _read_operating_income_series(qbo_client: QBOClient, db, company_id: str) -> list[float]:
    """The last completed month + the month before it → a 2-period operating-income
    series (operating income = gross_profit − operating_expenses = EBIT). Two
    bounded reads; if either fails the whole series degrades to indeterminate."""
    from ..analysis.ratios import compute_period_pl_kpis, resolve_period

    today = date.today()
    last_complete = date(today.year, today.month, 1) - timedelta(days=1)
    prior_complete = date(last_complete.year, last_complete.month, 1) - timedelta(days=1)
    prior_period = resolve_period(prior_complete.year, prior_complete.month, db=db, realm_id=company_id)
    current_period = resolve_period(last_complete.year, last_complete.month, db=db, realm_id=company_id)
    prior_pl = compute_period_pl_kpis(qbo_client, prior_period)
    current_pl = compute_period_pl_kpis(qbo_client, current_period)
    return [
        float(prior_pl["gross_profit"]) - float(prior_pl["operating_expenses"]),
        float(current_pl["gross_profit"]) - float(current_pl["operating_expenses"]),
    ]


def _read_runway(qbo_client: QBOClient, db, company_id: str) -> dict[str, object]:
    from ..analysis.predictive_cash import predict_13_week_cash

    result = predict_13_week_cash(qbo_client, db=db, realm_id=company_id)
    summary = result.get("summary", {})
    return {
        "runway_weeks": summary.get("runway_weeks"),
        "current_cash": result.get("starting_cash"),
        "lowest_balance": summary.get("lowest_balance"),
        "negative_cash_weeks": summary.get("negative_cash_weeks"),
    }

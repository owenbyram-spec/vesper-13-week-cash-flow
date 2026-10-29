"""
app/routers/covenants.py
─────────────────────────
Debt covenant tracking and compliance status.

GET    /covenants             List all covenants for a company
POST   /covenants             Create a new covenant
PUT    /covenants/{id}        Update a covenant
DELETE /covenants/{id}        Delete a covenant
GET    /covenants/status      Compute pass/warn/breach for all covenants (live from QBO)
GET    /covenants/accounts    Return COA account list for formula builder
POST   /covenants/suggest     Standard senior-loan covenant template (fixed list, NOT AI)
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone  # noqa: F401
from enum import StrEnum
from typing import Any, Dict, List, Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import CompanyEbitdaConfig, DebtCovenant, User
from ..auth import get_current_user_from_token
# PLAN-EXEMPT: plan 20260625-pilot-bug-fixes AC-6.2 (explicit per-route guard import); active-plan.json not synced into this worktree
from ..dependencies import check_entity_tenant_access, check_tenant_access, global_tenant_guard
from ..qbo_client import get_qbo_client_from_db
from ..services.ai_metric_builder import draft_covenant, validate_ai_draft
from ..services.audit import log_audit_event
from ..services.covenant_config import (
    CovenantConfigError,
    update_covenant_service,
    update_ebitda_config_service,
)
from ..services.errors import safe_error_detail, user_safe_http_exception
from ..services.redaction import redact_to_string

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/covenants", tags=["Covenants"])


# ─────────────────────────────────────────────────────────────────────────────
# Schemas
# ─────────────────────────────────────────────────────────────────────────────

# PLAN-EXEMPT: item-3 EBITDA add-backs (ratified 2026-06-25 "commit and ratify 3+4").
# One user-MAPPED EBITDA add-back line: a fixed amount OR a signed COA-account balance,
# TAGGED with the category it belongs to (SYNTHESIS 20260630 — the user maps which accounts
# are interest/tax/D&A; the compute sums exactly those by id, no P&L keyword inference). The
# ``interest`` category is reused verbatim as the DSCR debt-service interest term.
class CovenantAddback(BaseModel):
    label: str
    kind: str = "fixed"          # "fixed" | "account"
    amount: float | None = None  # used when kind == "fixed"
    account_ref: str | None = None  # QBO account id/name, used when kind == "account"
    sign: str = "add"            # "add" | "subtract"
    # "interest" | "tax" | "depreciation" | "amortization" | "other". Drives the categorized
    # EBITDA breakdown; "interest" also feeds the DSCR debt-service denominator.
    category: str = "other"


# AC-2 (R3): the ONE shared company-level EBITDA definition. GET returns it; PUT replaces it.
# ``addbacks`` is the SAME categorized line schema as the per-covenant ebitda_addbacks — the
# compute is unchanged, only the SOURCE moves to the company level.
class CompanyEbitdaConfigOut(BaseModel):
    company_id: str
    addbacks: list[CovenantAddback] = Field(default_factory=list)


class CompanyEbitdaConfigUpdate(BaseModel):
    company_id: str
    addbacks: list[CovenantAddback] = Field(default_factory=list)


class CovenantDirection(StrEnum):
    ABOVE = "ABOVE"
    BELOW = "BELOW"


def _load_company_ebitda_addbacks(db: Session, company_id: str) -> list | None:
    """Load the company-level EBITDA add-back lines (AC-2 cutover).

    Returns the parsed list from ``CompanyEbitdaConfig`` for ``company_id``, or None when no
    config is set (→ book EBITDA only). This is the SINGLE source of EBITDA add-backs for the
    covenant compute path; the per-covenant ``ebitda_addbacks`` is no longer read there.
    """
    cfg = db.query(CompanyEbitdaConfig).filter(
        CompanyEbitdaConfig.company_id == company_id
    ).one_or_none()
    if cfg is None or not cfg.addbacks:
        return None
    try:
        return json.loads(cfg.addbacks)
    except (json.JSONDecodeError, TypeError):
        return None


class CovenantCreate(BaseModel):
    company_id: str
    loan_name: str
    name: str
    # Built-ins: DSCR | DEBT_EBITDA | LEVERAGE | MIN_CASH | CURRENT_RATIO
    # Custom:    COA_FORMULA (requires custom_formula JSON)
    metric_type: str
    threshold: float
    breach_direction: CovenantDirection = CovenantDirection.BELOW
    # For COA_FORMULA: JSON string {"numerator":["id1","id2"],"denominator":["id3"]}
    custom_formula: str | None = None
    ebitda_addbacks: list[CovenantAddback] | None = None
    # Covenant-tracking Wave 1 (AC-3): the QBO liability account refs this DSCR covenant
    # services. DSCR principal-paid is summed over these from QBO BalanceSheet-as-of-date.
    debt_service_accounts: list[str] | None = None
    # AC-4: per-covenant WARN cushion fraction (e.g. 0.05 = 5%); None → the 0.10 default.
    warn_cushion: float | None = None

    @field_validator("breach_direction", mode="before")
    @classmethod
    def normalize_breach_direction(cls, value: object) -> object:
        """Preserve legacy case-insensitive clients while rejecting every unknown token."""
        return value.upper() if isinstance(value, str) else value


class CovenantUpdate(BaseModel):
    loan_name: str | None = None
    name: str | None = None
    metric_type: str | None = None
    threshold: float | None = None
    breach_direction: CovenantDirection | None = None
    custom_formula: str | None = None
    ebitda_addbacks: list[CovenantAddback] | None = None
    debt_service_accounts: list[str] | None = None
    warn_cushion: float | None = None
    active: bool | None = None

    @field_validator("breach_direction", mode="before")
    @classmethod
    def normalize_breach_direction(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value


class CovenantAIDraftRequest(BaseModel):
    company_id: str
    prompt: str


class CovenantVerdict(StrEnum):
    PASS = "PASS"
    WARN = "WARN"
    BREACH = "BREACH"
    UNKNOWN = "UNKNOWN"


class CovenantOverallStatus(StrEnum):
    PASS = "PASS"
    WARN = "WARN"
    BREACH = "BREACH"
    UNKNOWN = "UNKNOWN"
    NO_COVENANTS = "NO_COVENANTS"


class CovenantComputationState(StrEnum):
    NO_COVENANTS = "NO_COVENANTS"
    COMPUTING = "COMPUTING"
    POPULATED = "POPULATED"
    NO_DATA = "NO_DATA"
    ERROR = "ERROR"


class CovenantDataQuality(StrEnum):
    """Typed compatibility field retained for the existing full-covenants consumer."""

    SUCCESS = "success"
    COMPUTING = "computing"
    NO_DATA = "no_data"
    PROVIDER_ERROR = "provider_error"
    FORMULA_ERROR = "formula_error"


class CovenantStatusError(BaseModel):
    error_code: str
    user_message: str
    trace_id: str
    retryable: bool
    support_hint: str | None = None


class CovenantStatusRow(BaseModel):
    """One configured covenant plus its typed computation/result state."""

    id: int
    company_id: str
    loan_name: str
    name: str
    metric_type: str
    threshold: float
    breach_direction: CovenantDirection
    custom_formula: str | None = None
    ebitda_addbacks: list[CovenantAddback] = Field(default_factory=list)
    debt_service_accounts: list[str] = Field(default_factory=list)
    warn_cushion: float | None = None
    active: bool
    created_at: str | None = None
    current_value: float | None = None
    status: CovenantVerdict
    period: str
    breakdown: dict[str, Any] | None = None
    data_quality: CovenantDataQuality
    computation_state: CovenantComputationState
    errors: list[CovenantStatusError] = Field(default_factory=list)
    headroom: dict[str, Any] | None = None
    periods_until_breach: int | float | None = None
    projection: dict[str, Any]


class CovenantStatusResponse(BaseModel):
    """Stable edge contract for GET /status and the existing explicit refresh response."""

    company_id: str
    as_of: str
    period: str
    overall_status: CovenantOverallStatus
    computation_state: CovenantComputationState
    request_id: str
    warm_task_id: str | None
    retryable: bool
    errors: list[CovenantStatusError] = Field(default_factory=list)
    covenants: list[CovenantStatusRow] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_aggregate_computation_state(self) -> "CovenantStatusResponse":
        """Keep the top-level polling state coherent with the serialized row states."""
        if not self.covenants:
            if (
                self.computation_state != CovenantComputationState.NO_COVENANTS
                or self.overall_status != CovenantOverallStatus.NO_COVENANTS
            ):
                raise ValueError(
                    "an empty covenant status requires NO_COVENANTS computation and overall states"
                )
            if self.warm_task_id is not None or self.retryable or self.errors:
                raise ValueError(
                    "an empty NO_COVENANTS status must not carry task or error state"
                )
        else:
            if (
                self.overall_status == CovenantOverallStatus.NO_COVENANTS
                or self.computation_state == CovenantComputationState.NO_COVENANTS
            ):
                raise ValueError("NO_COVENANTS states require an empty covenant list")
            row_states = {row.computation_state for row in self.covenants}
            if CovenantComputationState.NO_COVENANTS in row_states:
                raise ValueError("covenant rows cannot use the NO_COVENANTS computation state")
            if CovenantComputationState.COMPUTING in row_states:
                expected_state = CovenantComputationState.COMPUTING
            elif CovenantComputationState.ERROR in row_states:
                expected_state = CovenantComputationState.ERROR
            elif row_states == {CovenantComputationState.NO_DATA}:
                expected_state = CovenantComputationState.NO_DATA
            else:
                expected_state = CovenantComputationState.POPULATED
            if self.computation_state != expected_state:
                raise ValueError(
                    "top-level computation state does not match covenant row precedence"
                )

        if self.computation_state == CovenantComputationState.COMPUTING and not self.warm_task_id:
            raise ValueError("COMPUTING covenant status requires warm_task_id")
        return self


# PLAN-EXEMPT: plan 20260625-pilot-bug-fixes AC-6.2 (preview endpoint); active-plan.json not synced into this worktree
class CovenantPreviewRequest(BaseModel):
    company_id: str
    # The SAME JSON shape the formula builder serializes + the row stores:
    # {"numerator":[...],"numerator_subtract":[...],"denominator":[...],"denominator_subtract":[...]}
    custom_formula: str


# PLAN-EXEMPT: plan 20260625-pilot-bug-fixes AC-6.2 (preview endpoint); active-plan.json not synced into this worktree
class CovenantPreviewResponse(BaseModel):
    value: float | None = None
    breakdown: dict | None = None
    period: str


# PLAN-EXEMPT: plan 20260630-covenant-redesign Wave-4 AC-1 (builder live preview); active-plan.json not synced into this worktree
# The UNSAVED covenant spec the builder serializes for the live two-lens monthly preview. It is
# the SAME field set create/update accept (metric_type / threshold / warn_cushion /
# debt_service_accounts / optional custom_formula) minus the persisted-row plumbing — so the
# preview reproduces exactly what the saved covenant would show, with no covenant row written.
class CovenantSeriesPreviewRequest(BaseModel):
    company_id: str
    metric_type: str
    threshold: float
    breach_direction: CovenantDirection = CovenantDirection.BELOW
    warn_cushion: float | None = None
    debt_service_accounts: list[str] = Field(default_factory=list)
    custom_formula: str | None = None
    # PLAN-EXEMPT: plan 20260630-covenant-redesign fix — the builder's mapped EBITDA add-back
    # accounts, so the live preview reflects the SAME interest/D&A/tax the saved covenant will use.
    interest_accounts: list[str] = Field(default_factory=list)
    da_accounts: list[str] = Field(default_factory=list)
    tax_accounts: list[str] = Field(default_factory=list)
    # Evaluate as of this closed month (YYYY-MM); default = last closed month. Mirrors /status.
    as_of: str | None = None

    @field_validator("breach_direction", mode="before")
    @classmethod
    def normalize_breach_direction(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value


# The one month-column of the two-lens grid — the JSON shape of covenant_compute.CovenantMonth
# (a typed edge model, not an untyped dict; honest degradation carried by ``no_data``).
class CovenantSeriesMonth(BaseModel):
    period: str
    net_income: float | None = None
    interest: float = 0.0
    taxes: float = 0.0
    d_and_a: float = 0.0
    ebitda: float | None = None
    principal: float | None = None
    debt_service: float | None = None
    total_debt: float | None = None
    run_rate_ratio: float | None = None
    overage: float | None = None
    no_data: str | None = None
    run_rate_not_meaningful: str | None = None


# PLAN-EXEMPT: plan 20260630-covenant-redesign AC-2 (screen == bank report; single source of truth)
# ONE display row of the bank-send monthly grid — the SAME shape covenant_monthly_rows.GridRow
# carries. The screen renders THIS row set, and the covenant_monthly PDF/Excel report renders the
# SAME rows from the SAME builder (rows_for), so the on-screen grid and the downloaded report
# cannot drift. ``kind`` is "money" | "ratio" (drives frontend formatting); ``emphasis`` bolds the
# ratio/covenant/tested rows exactly as the workbook does.
class CovenantGridRow(BaseModel):
    label: str
    kind: str
    months: list[float | None] = Field(default_factory=list)
    summary: float | None = None
    emphasis: bool = False


# The full two-lens series — the JSON shape of covenant_compute.CovenantSeries.to_dict().
# ``available`` / ``reason`` / per-month ``no_data`` carry honest degradation through unchanged.
# PLAN-EXEMPT: plan 20260630-covenant-redesign AC-2 — ``rows`` / ``month_labels`` / ``summary_header``
# expose the SHARED bank-report row plan (from app.reporting.covenant_monthly_rows) so the on-screen
# grid IS the report, not a hand-copied parallel layout.
class CovenantSeriesPreviewResponse(BaseModel):
    metric_type: str
    threshold: float
    breach_direction: CovenantDirection
    lens: str
    months: list[CovenantSeriesMonth] = Field(default_factory=list)
    ttm_ratio: float | None = None
    ttm_overage: float | None = None
    ttm_not_meaningful: str | None = None
    available: bool = False
    reason: str | None = None
    period: str
    # The bank-report row plan (single source of truth — screen == report).
    rows: list[CovenantGridRow] = Field(default_factory=list)
    month_labels: list[str] = Field(default_factory=list)
    summary_header: str = "Total"

    @field_validator("breach_direction", mode="before")
    @classmethod
    def normalize_breach_direction(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value


def _series_grid(series_dict: dict, threshold: float | None) -> dict:
    """AC-2: build the bank-report row plan for a computed series, from the ONE shared row
    definition the covenant_monthly PDF/Excel renderers use (``covenant_monthly_rows.rows_for``).

    Returns ``{rows, month_labels, summary_header}`` — the exact rows/labels/order the report
    renders — so the on-screen grid and the downloaded report render from one definition and
    cannot drift. DISPLAY-ONLY: it re-derives no covenant math, it only reshapes the already-
    computed series into display rows.
    """
    from ..reporting.covenant_monthly_rows import month_labels, rows_for, summary_header

    entry = {"series": series_dict, "threshold": threshold}
    return {
        "rows": [
            CovenantGridRow(
                label=r.label, kind=r.kind, months=list(r.months),
                summary=r.summary, emphasis=r.emphasis,
            )
            for r in rows_for(entry)
        ],
        "month_labels": month_labels(series_dict),
        "summary_header": summary_header(entry),
    }


def _canonical_covenant_direction(value: object) -> str:
    """Normalize a valid direction; reject unknown persisted/wire tokens explicitly."""
    direction = str(value or "").upper()
    if direction not in {item.value for item in CovenantDirection}:
        raise ValueError(f"Unsupported covenant breach direction: {direction or '<empty>'}")
    return direction


def _edge_covenant_direction(cov: DebtCovenant) -> str:
    """Return the canonical direction or a safe, visible configuration error at the HTTP edge."""
    try:
        return _canonical_covenant_direction(cov.breach_direction)
    except ValueError as exc:
        raise user_safe_http_exception(
            status_code=500,
            error_code="covenant_direction_invalid",
            user_message=(
                "A configured covenant has an invalid breach direction. Update the covenant "
                "before calculating its status."
            ),
            exc=exc,
            retryable=False,
            log_context="covenant_direction_error",
        ) from exc


def _cov_to_dict(c: DebtCovenant) -> Dict[str, Any]:
    return {
        "id": c.id,
        "company_id": c.company_id,
        "loan_name": c.loan_name,
        "name": c.name,
        "metric_type": c.metric_type,
        "threshold": float(c.threshold),
        "breach_direction": _canonical_covenant_direction(c.breach_direction),
        "custom_formula": c.custom_formula,
        # PLAN-EXEMPT: item-3 add-backs — surface the parsed lines to the editor UI (ratified 2026-06-25)
        "ebitda_addbacks": json.loads(c.ebitda_addbacks) if c.ebitda_addbacks else [],
        # Covenant-tracking Wave 1 (AC-3/AC-4): surface the mapped debt accounts + the cushion.
        # getattr-guarded so a row predating the migration (or a lightweight stub) serialises cleanly.
        "debt_service_accounts": json.loads(getattr(c, "debt_service_accounts", None)) if getattr(c, "debt_service_accounts", None) else [],
        "warn_cushion": float(c.warn_cushion) if getattr(c, "warn_cushion", None) is not None else None,
        "active": c.active,
        "created_at": c.created_at.isoformat() if c.created_at else None,
    }


def _cov_warn_cushion(c: DebtCovenant) -> float:
    """The covenant's WARN cushion fraction — the per-covenant value, or the 0.10 default (AC-4)."""
    cushion = getattr(c, "warn_cushion", None)
    return float(cushion) if cushion is not None else DEFAULT_WARN_CUSHION


# PLAN-EXEMPT: plan 20260630-covenant-redesign fix — EBITDA add-backs are per-covenant (the builder's
# interest/D&A/tax mapping on the covenant row); the company config is only a fallback for a covenant
# that mapped none. Keeps the /status header verdict consistent with the monthly grid + report.
def _cov_own_addbacks(c: DebtCovenant, company_addbacks: list | None) -> list | None:
    """The covenant's OWN mapped EBITDA add-back lines (``ebitda_addbacks``), or the company-level
    fallback when the covenant has none. Mirrors ``covenant_compute._series_addbacks`` so the
    single-period /status value and the two-lens monthly grid resolve EBITDA from one source."""
    raw = getattr(c, "ebitda_addbacks", None)
    if raw:
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return company_addbacks
        if parsed:
            return parsed
    return company_addbacks


def _audit_threshold_change(
    db: Session,
    cov: DebtCovenant,
    *,
    old_threshold: float | None,
    new_threshold: float,
    user: User,
) -> None:
    """AC-10: record a covenant threshold create/change to the existing audit trail (old→new
    value, covenant id, actor). Uses the shared ``log_audit_event`` path — it does NOT commit
    on its own, so the audit row lands in the SAME transaction as the covenant write (atomic:
    the threshold change and its audit row succeed or fail together). A create logs old=None.
    """
    action = "COVENANT_THRESHOLD_SET" if old_threshold is None else "COVENANT_THRESHOLD_CHANGED"
    user_id = getattr(user, "id", None)
    log_audit_event(
        db,
        company_id=cov.company_id,
        action=action,
        entity_type="DebtCovenant",
        entity_id=str(cov.id),
        user_id=user_id if isinstance(user_id, int) else None,
        previous_state={"threshold": old_threshold} if old_threshold is not None else None,
        new_state={
            "threshold": new_threshold,
            "name": cov.name,
            "loan_name": cov.loan_name,
            "metric_type": cov.metric_type,
            "breach_direction": cov.breach_direction,
        },
    )


def _load_account_catalog(db: Session, company_id: str) -> List[Dict[str, Any]]:
    from ..models import QBOAccount
    from ..services.coa_formula import get_chart_of_accounts

    rows = db.query(QBOAccount).filter(QBOAccount.realm_id == company_id, QBOAccount.active == True).all()  # noqa: E712
    if rows:
        return [
            {
                "id": row.qbo_id,
                "acct_num": row.acct_num,
                "name": row.name,
                "fully_qualified_name": row.fully_qualified_name,
                "type": row.account_type,
                "sub_type": row.account_sub_type,
                "classification": row.classification,
            }
            for row in rows
        ]

    client = get_qbo_client_from_db(db, company_id)
    if not client:
        raise HTTPException(status_code=400, detail="No QBO connection found.")
    try:
        return get_chart_of_accounts(client)
    except Exception as exc:
        raise user_safe_http_exception(
            status_code=502,
            error_code="qbo_provider_error",
            user_message="Chart of accounts could not be loaded from QuickBooks.",
            exc=exc,
            retryable=True,
            log_context="covenant_account_catalog_error",
        ) from exc


_FORMULA_ACCOUNT_KEYS = (
    "numerator", "numerator_subtract", "denominator", "denominator_subtract",
)


def _validate_formula_accounts(db: Session, company_id: str, custom_formula: str) -> None:
    """AC-4: validate every account ref in a custom COA formula exists in the company's COA at
    SAVE time. Raises 400 (naming the offending ref) so a typo'd account fails loudly now,
    instead of silently summing to 0 at month-end. A malformed-JSON formula raises 400 too.
    """
    try:
        formula = json.loads(custom_formula)
    except (json.JSONDecodeError, TypeError) as exc:
        http_exc = user_safe_http_exception(
            status_code=400,
            error_code="covenant_formula_invalid",
            user_message="This covenant formula is invalid and could not be evaluated.",
            exc=exc,
            retryable=False,
            log_context="covenant_formula_validate_error",
        )
        raise http_exc from exc
    refs = [
        str(ref).strip()
        for key in _FORMULA_ACCOUNT_KEYS
        for ref in (formula.get(key, []) or [])
        if str(ref).strip()
    ]
    if not refs:
        return
    catalog = _load_account_catalog(db, company_id)
    known = {str(a.get("id", "")).strip() for a in catalog}
    known |= {str(a.get("name", "")).strip() for a in catalog}
    known.discard("")
    missing = sorted({r for r in refs if r not in known})
    if missing:
        raise HTTPException(status_code=400, detail=safe_error_detail(
            error_code="covenant_formula_account_missing",
            user_message=f"This formula references account(s) not in the chart of accounts: {', '.join(missing)}.",
            retryable=False,
            log_context="covenant_formula_validate_error",
        ))


# ─────────────────────────────────────────────────────────────────────────────
# Status computation
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_WARN_CUSHION = 0.10  # AC-4: applied when a covenant has no per-covenant warn_cushion.


def _compute_status(
    value: float | None,
    threshold: float,
    breach_direction: str,
    cushion: float = DEFAULT_WARN_CUSHION,
    *,
    not_meaningful: str | None = None,
) -> str:
    """PASS / WARN / BREACH / UNKNOWN for ``value`` against the threshold + WARN cushion.

    ``not_meaningful`` is the code a compute records when the ratio's denominator is zero or
    negative (coa_formula.NOT_MEANINGFUL_REASONS): the true ratio is unbounded, so a maximum-ratio
    covenant (breach ABOVE) is BREACH; any other direction cannot be judged and stays UNKNOWN."""
    if value is None:
        if (
            not_meaningful
            and _canonical_covenant_direction(breach_direction) == CovenantDirection.ABOVE.value
        ):
            return "BREACH"
        return "UNKNOWN"
    if _canonical_covenant_direction(breach_direction) == CovenantDirection.BELOW.value:
        if value < threshold:
            return "BREACH"
        elif value < threshold * (1 + cushion):
            return "WARN"
        return "PASS"
    else:
        if value > threshold:
            return "BREACH"
        elif value > threshold * (1 - cushion):
            return "WARN"
        return "PASS"


def _compute_headroom(value: float | None, threshold: float, breach_direction: str) -> dict | None:
    """AC-5: signed cushion between the current value and the threshold.

    ``amount`` is the signed distance toward breach: POSITIVE = room to spare (passing),
    NEGATIVE = already past the threshold (in breach). For a BELOW-breach covenant the value
    must stay ABOVE the threshold, so room = value − threshold; for an ABOVE-breach covenant the
    value must stay BELOW it, so room = threshold − value. ``percent`` expresses that room as a
    fraction of the threshold (threshold == 0 → percent is None, undefined). Returns None when the
    value is unavailable — never a fabricated cushion off a substituted 0 (DEC-CAR-C-4).
    """
    if value is None:
        return None
    amount = (
        value - threshold
        if _canonical_covenant_direction(breach_direction) == CovenantDirection.BELOW.value
        else threshold - value
    )
    percent = round(amount / threshold, 4) if threshold else None
    return {
        "amount": round(amount, 4),
        "percent": percent,
        "breaching": amount < 0,
    }


def _covenant_projection(
    c: DebtCovenant,
    client,
    addbacks: list | None = None,
    *,
    as_of_date: date | None = None,
) -> dict:
    """AC-5: periods-until-breach for one covenant off the live trend (covenant_projection.py).

    Returns the projection dict as-is — ``available:false`` (+ a ``reason``) when the trend cannot
    be derived (no QBO client, a custom COA formula the trend projector does not evaluate, or the
    NO_DATA path), ``available:true`` with ``periods_until_breach`` when it can. The COMPANY-level
    EBITDA add-backs (AC-2) are threaded so the projected DSCR trend matches the /status DSCR. The
    reason is surfaced honestly, NOT dropped and NOT replaced with a fabricated slope (DEC-CAR-C-4).
    """
    if client is None:
        return {
            "available": False,
            "metric_type": c.metric_type,
            "reason": "No QuickBooks connection is available to project this covenant's trend.",
        }
    # The forward projector evaluates a built-in metric over two live windows; it does not parse a
    # custom COA formula, so a COA_FORMULA covenant is honestly could-not-verify (not a fake slope).
    if c.custom_formula:
        return {
            "available": False,
            "metric_type": c.metric_type,
            "reason": "Trend projection is not available for custom COA-formula covenants yet.",
        }
    from ..analysis.covenant_projection import covenant_periods_until_breach

    try:
        return covenant_periods_until_breach(
            client,
            c.metric_type,
            float(c.threshold),
            breach_direction=_canonical_covenant_direction(c.breach_direction),
            as_of_date=as_of_date,
            addbacks=addbacks,
        )
    except Exception as exc:  # the projector is best-effort context on /status — never break status
        logger.warning("[covenants] projection failed for covenant %s: %s", c.id, type(exc).__name__)
        return {
            "available": False,
            "metric_type": c.metric_type,
            "reason": "The trend for this covenant could not be projected from QuickBooks.",
        }


def _no_data_reason(breakdown: dict | None) -> tuple[str, str]:
    """Map a builtin's no-data breakdown to a specific (error_code, user_message) (AC-4).

    The builtin records a per-term ``error_code`` + ``reason`` on the numerator (EBITDA) or
    denominator (Debt Service) when it cannot compute; surface that instead of a generic blank.
    """
    default = (
        "covenant_metric_no_data",
        "This covenant metric could not be calculated from the available QuickBooks data.",
    )
    if not isinstance(breakdown, dict):
        return default
    for key in ("denominator", "numerator"):
        term = breakdown.get(key)  # SA-OK: bounded-loop 2 — dict.get over a fixed 2-key tuple, not a DB query
        if isinstance(term, dict) and term.get("value") is None and term.get("error_code"):
            return term["error_code"], term.get("reason") or default[1]
    if breakdown.get("error_code"):
        return breakdown["error_code"], breakdown.get("reason") or default[1]
    return default


def _merge_overall_status(current: str, status: str) -> str:
    if status == "BREACH":
        return "BREACH"
    if current == "BREACH":
        return current
    if status == "WARN":
        return "WARN"
    if current == "WARN":
        return current
    if status == "UNKNOWN" and current == "PASS":
        return "UNKNOWN"
    return current


def _last_day_of_month(year: int, month: int) -> "date":
    """Last calendar day of (year, month)."""
    from datetime import date, timedelta  # noqa: F811
    first_next = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return first_next - timedelta(days=1)


def _get_date_range(today: "date | None" = None, as_of: str | None = None) -> tuple[str, str]:
    """Covenant evaluation window = a closed-month trailing-twelve-month window, NOT a
    raw rolling 365 days ending today.

    Without ``as_of``: end_date = the last day of the prior (last fully-closed) calendar
    month — the default "last closed month" behaviour (unchanged). With ``as_of`` (a
    ``YYYY-MM`` string, AC-3): end_date = the last day of the SELECTED month, so a user can
    look at a prior closed period; start_date is always the first day of the 12-month window
    ending there. Flow metrics (EBITDA, debt service) are summed over a clean TTM; stock
    metrics (Total Debt, cash, current ratio) are read as-of end_date — a real month-end,
    a point-in-time boundary. Mirrors the canonical closed-month TTM construction in
    app/fpa/driver_mapping.py and the LBO init-from-qbo path (app/routers/lbo.py), so the
    repo shares ONE date scheme. ``today`` is injectable for deterministic golden tests.
    """
    from datetime import date, timedelta  # noqa: F811
    today = today or date.today()
    if as_of is not None:
        # end = last day of the selected month; start = 12 months back (first day).
        year, month = _parse_as_of(as_of, today=today)
        end_dt = _last_day_of_month(year, month)
    else:
        end_dt = date(today.year, today.month, 1) - timedelta(days=1)
    start_dt = (
        date(end_dt.year - 1, end_dt.month + 1, 1)
        if end_dt.month < 12
        else date(end_dt.year, 1, 1)
    )
    return start_dt.isoformat(), end_dt.isoformat()


def _parse_as_of(as_of: str, today: "date | None" = None) -> tuple[int, int]:
    """Parse + validate an ``as_of`` ``YYYY-MM`` query param (AC-3). Returns (year, month).

    Raises a 400 envelope on a malformed value (not ``YYYY-MM``, bad month) or a FUTURE month
    (a month whose last day is after today — a covenant period cannot be in the future). The
    caller propagates the HTTPException to the client; a bad ``as_of`` fails loudly, never
    silently defaults.
    """
    from datetime import date  # noqa: F811
    today = today or date.today()

    def _bad(reason: str) -> HTTPException:
        return HTTPException(status_code=400, detail=safe_error_detail(
            error_code="covenant_as_of_invalid",
            user_message=reason,
            retryable=False,
            log_context="covenant_as_of_invalid",
        ))

    parts = str(as_of).strip().split("-")
    if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
        raise _bad("The as-of period must be a month in YYYY-MM form (e.g. 2026-05).")
    year, month = int(parts[0]), int(parts[1])
    if not (1 <= month <= 12) or year < 1900:
        raise _bad("The as-of period must be a valid month in YYYY-MM form (e.g. 2026-05).")
    if _last_day_of_month(year, month) > today:
        raise _bad("The as-of period cannot be a future month.")
    return year, month


# PLAN-EXEMPT: plan 20260630-covenant-redesign fix — map a report period to a covenant as_of window.
def _period_to_as_of(period: str | None) -> str | None:
    """Map a report ``period`` (``YYYY-MM``) to a covenant ``as_of`` window, CLAMPING a
    current/future/malformed period to None (→ the last-closed-month default) instead of raising.
    Lets a ``covenant_monthly`` download honor a selected PAST month while never 400-ing on the
    live (in-progress) month — so the downloaded report matches the on-screen as-of grid."""
    from datetime import date  # noqa: F811
    if not period:
        return None
    parts = str(period).strip().split("-")
    if len(parts) < 2 or not parts[0].isdigit() or not parts[1].isdigit():
        return None
    year, month = int(parts[0]), int(parts[1])
    if not (1 <= month <= 12) or year < 1900:
        return None
    if _last_day_of_month(year, month) > date.today():
        return None
    return f"{year:04d}-{month:02d}"


# ─────────────────────────────────────────────────────────────────────────────
# Snapshot-first serving (F-2 #16)
# PLAN-EXEMPT: ratified plan 20260702-qa-remediation W-2 — /status + /{id}/series serve from the
# latest CovenantRecord snapshot (calculation_context carries the monthly grid) when fresh, and a
# live compute WRITES the snapshot so the next view is fast. Live compute_monthly_series remains
# the single engine; the snapshot is a byte-identical cache of its output, never a second formula.
# ─────────────────────────────────────────────────────────────────────────────

SNAPSHOT_FRESH_SECONDS = 24 * 3600  # a current-period snapshot older than this recomputes live
# The worker's capped retry countdowns are 180s + 360s; 30 minutes leaves ample compute/queue
# headroom while ensuring a lost/revoked task cannot leave the product permanently COMPUTING.
COVENANT_WARM_STALE_SECONDS = 30 * 60


def _default_snapshot_period() -> str:
    """The last fully-closed calendar month (``YYYY-MM``) — the period /status evaluates when no
    ``as_of`` is given. Reuses the beat task's helper so router + beat share ONE period scheme."""
    from ..tasks.covenant_snapshot import period_for_date

    return period_for_date()


def _snapshot_period(as_of: str | None) -> str:
    """The CovenantRecord ``period`` key (``YYYY-MM``) a request resolves to. ``as_of`` must
    already be validated (callers run ``_get_date_range(as_of=...)`` first)."""
    if as_of is None:
        return _default_snapshot_period()
    year, month = _parse_as_of(as_of)
    return f"{year:04d}-{month:02d}"


def _fresh_snapshot_record(db: Session, cov: DebtCovenant, period: str):
    """The covenant's stored two-lens snapshot for ``period``, IF it can serve the read path.

    Servable = the context is a REAL two-lens compute (lens set — the QBO-unreachable stub has
    ``lens: None`` and is never served), computed against the covenant's CURRENT threshold +
    breach direction (a re-thresholded covenant recomputes live), and FRESH: a historical period
    is settled bookkeeping (same-period suffices at any age), while the current default period can
    still move with in-progress bookkeeping so it must have been written <24h ago. Returns None
    on any miss → the caller computes live and rewrites the snapshot.

    FAIL-OPEN (B2-3): returns None on ANY exception too (a malformed stored context, an unexpected
    attribute) — a broken cache row must degrade to a live compute, never raise a 500."""
    from datetime import datetime, timedelta, timezone  # noqa: F811

    from ..models import CovenantRecord

    try:
        rec = (
            db.query(CovenantRecord)
            .filter(
                CovenantRecord.company_id == cov.company_id,
                CovenantRecord.covenant_id == cov.id,
                CovenantRecord.period == period,
            )
            .first()
        )
        if rec is None:
            return None
        ctx = rec.calculation_context
        if isinstance(ctx, dict):
            computation = ctx.get("computation")
            if isinstance(computation, dict) and computation.get("state") in {
                CovenantComputationState.COMPUTING.value,
                CovenantComputationState.ERROR.value,
            }:
                # A holder/error row may preserve an older financial context, but it is not a
                # serveable fresh result until the correlated worker reaches POPULATED/NO_DATA.
                return None
        if not isinstance(ctx, dict) or ctx.get("lens") not in ("coverage", "leverage"):
            return None
        stored_ttm = ctx.get("ttm")
        if not isinstance(stored_ttm, dict) or "not_meaningful" not in stored_ttm:
            # Written before not-meaningful ratios were recorded: such a row may hold a negative
            # Debt/EBITDA judged PASS. Never serve it — the live compute rewrites it.
            return None
        try:
            stored_threshold = float(ctx.get("threshold"))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
        current_threshold = float(cov.threshold) if cov.threshold is not None else 0.0
        if stored_threshold != current_threshold:
            return None
        if _canonical_covenant_direction(ctx.get("breach_direction")) != _canonical_covenant_direction(
            cov.breach_direction
        ):
            return None
        if period == _default_snapshot_period():
            updated = rec.updated_at or rec.created_at
            if updated is None:
                return None
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) - updated > timedelta(seconds=SNAPSHOT_FRESH_SECONDS):
                return None
        return rec
    except Exception as exc:  # noqa: BLE001 — a malformed/unreadable snapshot degrades to live compute
        logger.warning("[covenants] snapshot read failed (falling back to live) cov=%s period=%s: %s",
                       cov.id, period, redact_to_string(str(exc), max_length=300))
        return None


def _series_dict_from_snapshot(cov: DebtCovenant, ctx: dict) -> dict:
    """Reconstruct the ``CovenantSeries.to_dict()`` shape from a stored calculation_context —
    the EXACT dict the live path feeds ``CovenantSeriesPreviewResponse`` + ``_series_grid``, so
    snapshot-served and live-computed responses are byte-identical for the same data.

    FAIL-OPEN (B2-3): defensive about malformed sub-fields — a non-dict ``ttm`` or a non-iterable
    ``months`` degrades to empty rather than raising, so a partially-corrupt cache row cannot 500
    the read path (the caller further treats any raise here as a snapshot-miss → live compute)."""
    ttm = ctx.get("ttm")
    if not isinstance(ttm, dict):
        ttm = {}
    raw_months = ctx.get("months")
    months = [dict(m) for m in raw_months if isinstance(m, dict)] if isinstance(raw_months, list) else []
    return {
        "metric_type": str(cov.metric_type or "").upper(),
        "threshold": ctx.get("threshold"),
        "breach_direction": ctx.get("breach_direction"),
        "lens": ctx.get("lens"),
        "months": months,
        "ttm_ratio": ttm.get("ratio"),
        "ttm_overage": ttm.get("overage"),
        "ttm_not_meaningful": ttm.get("not_meaningful"),
        "available": bool(ctx.get("available")),
        "reason": ctx.get("reason"),
    }


def _write_covenant_snapshot(
    db: Session, cov: DebtCovenant, *, series, period: str,
    projection: dict | None = None, commit: bool = True,
) -> None:
    """Persist a freshly-computed series as the (covenant, period) CovenantRecord so the NEXT
    view serves from the snapshot. Reuses the beat task's context builder + idempotent upsert —
    one storage shape, no fork. Deliberately fires NO alerts (the beat owns lens-transition
    alerting; this is a read-path cache write).

    FAIL-OPEN (B2-3): this is a read-path cache write — its ONLY job is to make the NEXT view
    fast. A failure here (a lost unique-race the savepoint could not resolve, a commit error)
    must NEVER 500 the GET that just computed the series live. So the whole body is wrapped:
    on any exception the freshly-computed series is still returned to the caller (uncached),
    the cause is logged through the redactor, and the session is rolled back to a clean state."""
    from ..tasks.covenant_snapshot import _build_context, _latest_run_rate, _upsert_record

    try:
        threshold = float(cov.threshold) if cov.threshold is not None else 0.0
        cushion = _cov_warn_cushion(cov)
        ttm_value = series.ttm_ratio if series is not None else None
        ttm_status = _compute_status(
            ttm_value, threshold, cov.breach_direction, cushion=cushion,
            not_meaningful=series.ttm_not_meaningful if series is not None else None,
        )
        run_rate_value, run_rate_not_meaningful = _latest_run_rate(series)
        run_rate_status = _compute_status(
            run_rate_value, threshold, cov.breach_direction, cushion=cushion,
            not_meaningful=run_rate_not_meaningful,
        )
        context = _build_context(
            series, ttm_status, run_rate_status, run_rate_value, projection=projection,
            run_rate_not_meaningful=run_rate_not_meaningful,
        )
        _upsert_record(
            db, covenant_id=cov.id, company_id=cov.company_id, period=period,
            value=ttm_value, status=ttm_status, context=context,
        )
        if commit:
            db.commit()
    except Exception as exc:  # noqa: BLE001 — cache write is best-effort; a failure never 500s the GET
        logger.warning(
            "[covenants] snapshot cache write failed (series still served) cov=%s period=%s: %s",
            cov.id, period, redact_to_string(str(exc), max_length=300),
        )
        try:
            # LAYER-OK: best-effort rollback of THIS read-path cache-write helper (pairs with its
            # already-in-HEAD db.commit); restores the session so the live-served GET does not 500.
            db.rollback()
        except Exception as rb_exc:  # noqa: BLE001 — best-effort rollback of an already-failed cache write
            logger.warning("[covenants] rollback after failed snapshot cache write also failed: %s",
                           redact_to_string(str(rb_exc), max_length=300))


@dataclass(frozen=True)
class _WarmDispatchResult:
    state: CovenantComputationState
    task_id: str | None
    error: dict[str, Any] | None
    enqueued: bool
    covenant_ids: tuple[int, ...] = ()


def _persisted_warm_error(metadata: dict) -> dict[str, Any]:
    """Return only the safe error-envelope fields from a persisted terminal state."""
    error = metadata.get("error")
    if isinstance(error, dict) and all(
        isinstance(error.get(key), expected)
        for key, expected in (
            ("error_code", str),
            ("user_message", str),
            ("trace_id", str),
            ("retryable", bool),
        )
    ):
        return {
            "error_code": error["error_code"],
            "user_message": error["user_message"],
            "trace_id": error["trace_id"],
            "retryable": error["retryable"],
            **({"support_hint": error["support_hint"]} if error.get("support_hint") else {}),
        }
    return safe_error_detail(
        error_code="covenant_warm_failed",
        user_message="Covenant calculations failed. Retry the calculation.",
        retryable=True,
        log_context="covenant_warm_state_error",
    )


def _enqueue_covenant_warm(
    db: Session,
    *,
    company_id: str,
    period: str,
    covenant_ids: list[int],
    request_id: str,
    retry_failed: bool,
) -> _WarmDispatchResult:
    """Commit one tenant+period holder, then enqueue its preassigned Celery task id.

    The active covenant rows are locked while the holder is checked/created.  On Postgres this
    serializes concurrent cold polls for the same realm, so only the winner enqueues; followers see
    its persisted COMPUTING task id.  A persisted ERROR is terminal for ordinary polls and is
    re-armed only by the explicit ``retry_failed=true`` GET.
    """
    from ..tasks.covenant_snapshot import (
        COMPUTATION_COMPUTING,
        COMPUTATION_ERROR,
        COMPUTATION_NO_DATA,
        COMPUTATION_POPULATED,
        _mark_warm_error_if_owner,
        _records_for_warm,
        mark_warm_computing,
        snapshot_realm_covenants,
        warm_metadata_for_covenant,
    )
    from ..services.covenant_compute import SUPPORTED_METRICS

    if not covenant_ids:
        return _WarmDispatchResult(
            state=CovenantComputationState.NO_DATA,
            task_id=None,
            error=None,
            enqueued=False,
        )

    # Lock the FULL supported set, regardless of whether /status or one /series card found the
    # miss.  Every child computes the whole realm, so a per-card subset would let two cards lock
    # disjoint rows and enqueue duplicate realm+period tasks.  Only the caller's missing rows need
    # a new holder; the full set is the shared single-flight coordination surface.
    locked_realm = (
        db.query(DebtCovenant)
        .filter(
            DebtCovenant.company_id == company_id,
            DebtCovenant.active == True,  # noqa: E712
        )
        .order_by(DebtCovenant.id)
        .with_for_update()
        .all()
    )
    locked_ids = [
        int(cov.id)
        for cov in locked_realm
        if str(cov.metric_type or "").upper() in SUPPORTED_METRICS
    ]
    holder_ids = sorted(set(covenant_ids).intersection(locked_ids))
    if not holder_ids:
        db.commit()
        return _WarmDispatchResult(
            state=CovenantComputationState.NO_DATA,
            task_id=None,
            error=None,
            enqueued=False,
        )
    records = _records_for_warm(
        db, company_id=company_id, period=period, covenant_ids=locked_ids
    )
    metadata_by_covenant = {
        covenant_id: rec.calculation_context.get("computation")
        for covenant_id, rec in records.items()
        if isinstance(rec.calculation_context, dict)
        and isinstance(rec.calculation_context.get("computation"), dict)
    }

    # A task can disappear after its holder commit (worker loss/revoke/broker data loss).  Treat
    # the holder as a bounded lease: terminalize stale rows under this same lock so `/series`, which
    # enters directly through this helper, cannot remain COMPUTING forever.
    stale_pending = {
        covenant_id: item
        for covenant_id, item in metadata_by_covenant.items()
        if item.get("state") == COMPUTATION_COMPUTING and _warm_holder_is_stale(item)
    }
    for covenant_id, stale_metadata in stale_pending.items():
        metadata_by_covenant[covenant_id] = _terminalize_stale_warm(
            db,
            company_id=company_id,
            period=period,
            covenant_ids=[covenant_id],
            metadata=stale_metadata,
            commit=False,
        )

    metadata = list(metadata_by_covenant.values())
    pending = next(
        (item for item in metadata if item.get("state") == COMPUTATION_COMPUTING),
        None,
    )
    if pending is not None:
        pending_task_id = str(pending.get("task_id"))
        owned_ids = tuple(
            sorted(
                covenant_id
                for covenant_id, item in metadata_by_covenant.items()
                if item.get("state") == COMPUTATION_COMPUTING
                and str(item.get("task_id") or "") == pending_task_id
            )
        )
        db.commit()
        return _WarmDispatchResult(
            state=CovenantComputationState.COMPUTING,
            task_id=pending_task_id,
            error=None,
            enqueued=False,
            covenant_ids=owned_ids,
        )

    # Terminal rows outside this caller's requested holder set must not suppress unrelated cold
    # work. Likewise, ``retry_failed=true`` is authority to re-arm only an explicitly retryable
    # failure; a permanent configuration/auth failure remains terminal even for a direct caller.
    blocked_terminals: list[tuple[int, dict[str, Any], dict[str, Any]]] = []
    for covenant_id in holder_ids:
        item = metadata_by_covenant.get(covenant_id)
        if not isinstance(item, dict) or item.get("state") != COMPUTATION_ERROR:
            continue
        persisted_error = _persisted_warm_error(item)
        if retry_failed and persisted_error["retryable"]:
            continue
        blocked_terminals.append((covenant_id, item, persisted_error))

    blocked_ids = {covenant_id for covenant_id, _, _ in blocked_terminals}
    holder_ids = [covenant_id for covenant_id in holder_ids if covenant_id not in blocked_ids]
    if not holder_ids and blocked_terminals:
        _, terminal, terminal_error = blocked_terminals[0]
        terminal_task_id = str(terminal.get("task_id")) if terminal.get("task_id") else None
        terminal_ids = tuple(
            sorted(
                covenant_id
                for covenant_id, item, _ in blocked_terminals
                if (
                    str(item.get("task_id")) if item.get("task_id") else None
                ) == terminal_task_id
            )
        )
        db.commit()
        return _WarmDispatchResult(
            state=CovenantComputationState.ERROR,
            task_id=terminal_task_id,
            error=terminal_error,
            enqueued=False,
            covenant_ids=terminal_ids,
        )

    task_id = str(uuid4())
    try:
        mark_warm_computing(
            db,
            company_id=company_id,
            period=period,
            covenant_ids=holder_ids,
            request_id=request_id,
            task_id=task_id,
        )
        # Load-bearing ordering: the product-visible holder exists before a broker can receive work.
        db.commit()
    except Exception as exc:  # noqa: BLE001 — state-write boundary; never enqueue unobservable work
        db.rollback()
        error = safe_error_detail(
            error_code="covenant_warm_state_unavailable",
            user_message=(
                "Covenant calculations could not be started safely. Retry the calculation."
            ),
            exc=exc,
            retryable=True,
            log_context="covenant_warm_holder_error",
        )
        return _WarmDispatchResult(
            state=CovenantComputationState.ERROR,
            task_id=None,
            error=error,
            enqueued=False,
            covenant_ids=tuple(holder_ids),
        )
    try:
        result = snapshot_realm_covenants.apply_async(
            kwargs={"qbo_realm_id": company_id, "period": period},
            task_id=task_id,
        )
        result_id = str(getattr(result, "id", None) or task_id)
        return _WarmDispatchResult(
            state=CovenantComputationState.COMPUTING,
            task_id=result_id,
            error=None,
            enqueued=True,
            covenant_ids=tuple(holder_ids),
        )
    except Exception as exc:  # noqa: BLE001 — broker boundary; converted to a safe persisted ERROR
        error = safe_error_detail(
            error_code="covenant_warm_enqueue_failed",
            user_message=(
                "Covenant calculations could not be queued. Retry when background processing "
                "is available."
            ),
            exc=exc,
            retryable=True,
            log_context="covenant_warm_enqueue_error",
        )
        # Publishing happens after the holder commit and therefore after the coordination lock is
        # released. A slow broker failure can arrive after a stale lease was re-armed by task B;
        # terminalize task A only through the same task-id CAS used by worker failures, then return
        # the authoritative persisted state instead of overwriting B/fresh work.
        _mark_warm_error_if_owner(
            db,
            company_id=company_id,
            period=period,
            covenant_ids=holder_ids,
            task_id=task_id,
            error=error,
        )
        authoritative = {
            covenant_id: warm_metadata_for_covenant(
                db,
                company_id=company_id,
                covenant_id=covenant_id,
                period=period,
            )
            for covenant_id in holder_ids
        }
        for state, response_state in (
            (COMPUTATION_COMPUTING, CovenantComputationState.COMPUTING),
            (COMPUTATION_ERROR, CovenantComputationState.ERROR),
            (COMPUTATION_POPULATED, CovenantComputationState.POPULATED),
            (COMPUTATION_NO_DATA, CovenantComputationState.NO_DATA),
        ):
            match = next(
                (
                    item
                    for item in authoritative.values()
                    if isinstance(item, dict) and item.get("state") == state
                ),
                None,
            )
            if match is None:
                continue
            authoritative_task_id = (
                str(match.get("task_id")) if match.get("task_id") else None
            )
            matching_ids = tuple(
                sorted(
                    covenant_id
                    for covenant_id, item in authoritative.items()
                    if isinstance(item, dict)
                    and item.get("state") == state
                    and (
                        str(item.get("task_id")) if item.get("task_id") else None
                    ) == authoritative_task_id
                )
            )
            return _WarmDispatchResult(
                state=response_state,
                task_id=authoritative_task_id,
                error=_persisted_warm_error(match) if state == COMPUTATION_ERROR else None,
                enqueued=False,
                covenant_ids=matching_ids,
            )
        return _WarmDispatchResult(
            state=CovenantComputationState.ERROR,
            task_id=task_id,
            error=error,
            enqueued=False,
            covenant_ids=tuple(holder_ids),
        )


def _warm_holder_is_stale(metadata: dict) -> bool:
    """Whether a COMPUTING holder has outlived the worker retry envelope."""
    if not metadata.get("task_id"):
        return True
    raw = metadata.get("updated_at") or metadata.get("started_at")
    if not raw:
        return True
    try:
        stamp = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return True
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - stamp).total_seconds() > COVENANT_WARM_STALE_SECONDS


def _terminalize_stale_warm(
    db: Session,
    *,
    company_id: str,
    period: str,
    covenant_ids: list[int],
    metadata: dict,
    commit: bool = True,
) -> dict[str, Any]:
    """Persist and return the safe terminal state for an expired COMPUTING lease."""
    from ..tasks.covenant_snapshot import mark_warm_error

    terminal = _stale_warm_terminal(metadata)
    error = terminal["error"]
    assert isinstance(error, dict)  # internal state built by _stale_warm_terminal
    mark_warm_error(
        db,
        company_id=company_id,
        period=period,
        covenant_ids=covenant_ids,
        request_id=str(metadata.get("request_id")) if metadata.get("request_id") else None,
        task_id=str(metadata.get("task_id") or ""),
        error=error,
    )
    if commit:
        db.commit()
    return terminal


def _stale_warm_terminal(metadata: dict) -> dict[str, Any]:
    """Build the safe terminal view for an expired lease without mutating the database."""
    from ..tasks.covenant_snapshot import COMPUTATION_ERROR

    error = safe_error_detail(
        error_code="covenant_warm_stale",
        user_message=(
            "The covenant calculation did not finish. Retry to start a new calculation."
        ),
        retryable=True,
        log_context="covenant_warm_stale_error",
    )
    return {
        **metadata,
        "state": COMPUTATION_ERROR,
        "retryable": True,
        "error": error,
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }


def _warm_metadata_for_status(
    db: Session,
    *,
    cov: DebtCovenant,
    period: str,
) -> dict | None:
    """Read one holder and surface an expired lease without an out-of-lock write.

    Persisted stale transition/re-arm belongs exclusively to ``_enqueue_covenant_warm`` under its
    full-realm row lock.  Writing here creates a TOCTOU where two explicit retries can overwrite a
    newly re-armed holder and publish twice.
    """
    from ..tasks.covenant_snapshot import COMPUTATION_COMPUTING, warm_metadata_for_covenant

    metadata = warm_metadata_for_covenant(
        db,
        company_id=cov.company_id,
        covenant_id=cov.id,
        period=period,
    )
    if (
        metadata is None
        or metadata.get("state") != COMPUTATION_COMPUTING
        or not _warm_holder_is_stale(metadata)
    ):
        return metadata

    return {**_stale_warm_terminal(metadata), "requires_coordination": True}


def _series_status_breakdown(
    *, available: bool, not_meaningful: object, reason: str | None,
) -> dict | None:
    """The /status breakdown for a two-lens TTM result: None when the ratio stands, the
    not-meaningful marker when trailing-12 EBITDA <= 0 (a verdict without a value), else the
    honest series-unavailable reason."""
    from ..services.coa_formula import NOT_MEANINGFUL_REASONS, mark_not_meaningful

    if isinstance(not_meaningful, str) and not_meaningful in NOT_MEANINGFUL_REASONS:
        return mark_not_meaningful({}, not_meaningful)
    if available:
        return None
    return {
        "error_code": "covenant_series_unavailable",
        "reason": reason or "This covenant could not be computed from QuickBooks.",
    }


def _two_lens_status_values(
    db: Session, cov: DebtCovenant, client, company_addbacks: list | None,
    start_date: str, end_date: str, *, period: str, force_live: bool = False,
    allow_live: bool = True, retry_failed: bool = False,
) -> tuple[float | None, dict | None, dict, dict[str, Any]]:
    """(value, breakdown, projection) for a DSCR / Debt-EBITDA covenant on /status —
    snapshot-first. A fresh (covenant, period) snapshot serves all three with zero QBO calls
    (a legacy row missing the stored projection heals itself in place); otherwise the live
    two-lens engine runs and the result is written back as the snapshot.

    BOUNDED-READ (F-2 stall fix): the INTERACTIVE GET path passes ``allow_live=False`` — a fresh
    snapshot is ALWAYS served, but on a snapshot MISS this returns a "computing" sentinel
    (``breakdown["error_code"] == "covenant_computing"``) INSTEAD of running the cumulative live
    two-lens compute (~0.4s/QBO-call × 12 months × N two-lens covenants — the confirmed
    browser-timeout stall). The caller enqueues an async warm on that sentinel. ``allow_live=True``
    (the default, and POST /covenants/refresh's ``force_live``) is UNCHANGED: live compute + snapshot
    write-back on a miss."""
    from ..services.covenant_compute import compute_monthly_series
    from ..tasks.covenant_snapshot import (
        COMPUTATION_COMPUTING,
        COMPUTATION_ERROR,
        COMPUTATION_NO_DATA,
        COMPUTATION_POPULATED,
    )

    warm_metadata = None if force_live else _warm_metadata_for_status(
        db, cov=cov, period=period
    )
    warm_state = (warm_metadata or {}).get("state")
    if not allow_live and warm_state == COMPUTATION_COMPUTING:
        return (
            None,
            {
                "error_code": "covenant_computing",
                "reason": "Recalculating from QuickBooks…",
                "enqueue_required": False,
            },
            {"available": False, "reason": "computing"},
            {**(warm_metadata or {}), "needs_enqueue": False},
        )
    if not allow_live and warm_state == COMPUTATION_ERROR:
        persisted_error = _persisted_warm_error(warm_metadata or {})
        requires_coordination = bool((warm_metadata or {}).get("requires_coordination"))
        if retry_failed and persisted_error["retryable"]:
            return (
                None,
                {
                    "error_code": "covenant_computing",
                    "reason": "Recalculating from QuickBooks…",
                    "enqueue_required": True,
                },
                {"available": False, "reason": "computing"},
                {
                    **(warm_metadata or {}),
                    "state": COMPUTATION_COMPUTING,
                    "needs_enqueue": True,
                },
            )
        return (
            None,
            {
                "error_code": persisted_error["error_code"],
                "reason": persisted_error["user_message"],
                "persisted_error": persisted_error,
                "enqueue_required": requires_coordination,
            },
            {"available": False, "reason": "error"},
            {**(warm_metadata or {}), "needs_enqueue": requires_coordination},
        )

    rec = None if force_live else _fresh_snapshot_record(db, cov, period)
    if rec is not None:
        # PLAN-EXEMPT: plan 20260702-qa-batch2 B2-3 (fail-open the covenant snapshot read); the
        # plan.md surface_scope lists app/routers/covenants.py but active-plan.json is not synced
        # into this worktree, so the gate's surface match fails on the worktree absolute path only.
        # FAIL-OPEN (B2-3): serve from the snapshot, but on ANY exception (a malformed stored
        # context, a projection-heal commit racing another write) fall THROUGH to the live compute
        # below rather than raising — a broken cache row must degrade to live, never 500 /status.
        try:
            ctx = rec.calculation_context
            if not isinstance(ctx, dict):
                raise ValueError("snapshot context is not a dict")
            ttm = ctx.get("ttm")
            # PLAN-EXEMPT: CI-red hotfix (Steven-directed) — a non-dict ttm is a CORRUPT
            # snapshot, not "value unknown": mapping it to None served a fabricated-empty
            # covenant from a broken cache row. Raise into the fail-open arm below so the
            # live compute runs and the write-back self-heals the row (B2-3 AC-3).
            if not isinstance(ttm, dict):
                raise ValueError("snapshot ttm is not a dict")
            value = ttm.get("ratio")
            breakdown = _series_status_breakdown(
                available=bool(ctx.get("available")),
                not_meaningful=ttm.get("not_meaningful"),
                reason=ctx.get("reason"),
            )
            projection = ctx.get("projection")
            if not isinstance(projection, dict):
                # Pre-W-2 snapshot rows carry no projection — compute the trend once and store it.
                # Best-effort heal: a commit failure here must not abort the serve (it falls open).
                projection = _covenant_projection(
                    cov,
                    client,
                    addbacks=_cov_own_addbacks(cov, company_addbacks),
                    as_of_date=date.fromisoformat(end_date),
                )
                rec.calculation_context = {**ctx, "projection": projection}
                db.commit()
            computation = ctx.get("computation")
            if not isinstance(computation, dict):
                computation = {
                    "state": (
                        COMPUTATION_POPULATED
                        if value is not None or ttm.get("not_meaningful")
                        else COMPUTATION_NO_DATA
                    ),
                    "task_id": None,
                    "request_id": None,
                    "retryable": False,
                    "error": None,
                }
            return value, breakdown, projection, {**computation, "needs_enqueue": False}
        except Exception as exc:  # noqa: BLE001 — a corrupt/unhealable snapshot degrades to a live compute
            logger.warning(
                "[covenants] snapshot status read failed (falling back to live) cov=%s period=%s: %s",
                cov.id, period, redact_to_string(str(exc), max_length=300),
            )
            try:
                # LAYER-OK: restore the session after a failed best-effort projection-heal commit so
                # the live compute + write-back below runs clean; pairs with the heal commit above.
                db.rollback()
            except Exception as rb_exc:  # noqa: BLE001 — best-effort rollback; the live path re-establishes state
                logger.warning("[covenants] rollback after failed snapshot status read also failed: %s",
                               redact_to_string(str(rb_exc), max_length=300))

    # BOUNDED-READ (F-2 stall fix): snapshot MISS on the interactive GET path — do NOT run the
    # cumulative live two-lens compute on the request thread (that is the browser-timeout stall).
    # Return a "computing" sentinel; the caller (_covenant_status_payload) enqueues an async warm.
    if not allow_live:
        return (
            None,
            {
                "error_code": "covenant_computing",
                "reason": "Recalculating from QuickBooks…",
                "enqueue_required": True,
            },
            {"available": False, "reason": "computing"},
            {
                "state": COMPUTATION_COMPUTING,
                "task_id": None,
                "request_id": None,
                "retryable": False,
                "error": None,
                "needs_enqueue": True,
            },
        )

    series = compute_monthly_series(cov, client, company_addbacks, start_date, end_date)
    projection = _covenant_projection(
        cov,
        client,
        addbacks=_cov_own_addbacks(cov, company_addbacks),
        as_of_date=date.fromisoformat(end_date),
    )
    _write_covenant_snapshot(db, cov, series=series, period=period, projection=projection)
    value = series.ttm_ratio
    breakdown = _series_status_breakdown(
        available=series.available,
        not_meaningful=series.ttm_not_meaningful,
        reason=series.reason,
    )
    return (
        value,
        breakdown,
        projection,
        {
            "state": (
                COMPUTATION_POPULATED
                if value is not None or series.ttm_not_meaningful
                else COMPUTATION_NO_DATA
            ),
            "task_id": None,
            "request_id": None,
            "retryable": False,
            "error": None,
            "needs_enqueue": False,
        },
    )


# ─────────────────────────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/accounts")
def get_coa_accounts(
    company_id: str = Query(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
) -> List[Dict[str, Any]]:
    """Return the full Chart of Accounts for the formula builder dropdowns."""
    check_tenant_access("VIEWER", company_id=company_id, db=db, user=current_user)
    from ..services.coa_formula import get_chart_of_accounts
    client = get_qbo_client_from_db(db, company_id)
    if not client:
        raise HTTPException(status_code=400, detail="No QBO connection found.")
    try:
        return get_chart_of_accounts(client)
    except Exception as exc:
        raise user_safe_http_exception(
            status_code=502,
            error_code="qbo_provider_error",
            user_message="Chart of accounts could not be loaded from QuickBooks.",
            exc=exc,
            retryable=True,
            log_context="covenant_coa_accounts_error",
        ) from exc


def _request_id_for(request: Request | None) -> str:
    """Use middleware identity at HTTP edge; generate one only for internal/direct callers."""
    if request is not None:
        request_id = getattr(request.state, "request_id", None)
        if request_id:
            return str(request_id)
        header_id = request.headers.get("x-request-id")
        if header_id:
            return header_id
    return str(uuid4())


@router.get("/status", response_model=CovenantStatusResponse)
def get_covenant_status(
    request: Request,
    company_id: str = Query(...),
    as_of: str | None = Query(None, description="Evaluate as of this closed month (YYYY-MM); default = last closed month."),
    retry_failed: bool = Query(
        False,
        description="Explicitly retry a persisted terminal covenant warm failure.",
    ),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
) -> dict[str, Any]:
    """
    Compute pass/warn/breach for all active covenants using live QBO data.
    Built-in metric types are computed directly; COA_FORMULA types are evaluated
    by summing account balances from the QBO General Ledger.

    ``as_of`` (AC-3) selects the closed month to evaluate (YYYY-MM); the TTM window ends on
    the last day of that month. Absent → the last closed month. Malformed/future → 400.
    EBITDA add-backs come from the ONE company-level config (AC-2), applied to every DSCR /
    Debt/EBITDA covenant.

    W-2 F-2: DSCR / Debt-EBITDA verdicts serve from the stored (covenant, period) snapshot when
    fresh (<24h for the current period; any age for a settled historical period). BOUNDED-READ:
    on a snapshot MISS this interactive GET never runs the live two-lens compute on the request
    thread — it returns that covenant as ``data_quality="computing"`` (status UNKNOWN, excluded
    from the overall merge) and enqueues ONE async warm so the next view serves from a fresh
    snapshot. POST /covenants/refresh is the explicit live recompute.
    """
    # PLAN-EXEMPT: cherry-pick conflict resolution landing reviewed market-ready commits onto main
    check_tenant_access("VIEWER", company_id=company_id, db=db, user=current_user)
    return _covenant_status_payload(
        db,
        company_id,
        as_of,
        request_id=_request_id_for(request),
        retry_failed=retry_failed,
    )


# PLAN-EXEMPT: ratified plan 20260702-qa-remediation W-2 (F-2 explicit on-demand recompute)
@router.post("/refresh", response_model=CovenantStatusResponse)
def refresh_covenant_status(
    request: Request,
    company_id: str = Query(...),
    as_of: str | None = Query(None, description="Recompute as of this closed month (YYYY-MM); default = last closed month."),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
) -> dict[str, Any]:
    """W-2 F-2: explicit on-demand recompute. Forces the LIVE two-lens compute for every active
    covenant (bypassing the snapshot read path), REWRITES each (covenant, period) snapshot, and
    returns the same payload GET /covenants/status serves. VIEWER-gated: it runs exactly the
    compute a stale GET would run and writes only the read-path cache, no user data."""
    check_tenant_access("VIEWER", company_id=company_id, db=db, user=current_user)
    return _covenant_status_payload(
        db,
        company_id,
        as_of,
        force_live=True,
        request_id=_request_id_for(request),
    )


def _covenant_status_payload(
    db: Session,
    company_id: str,
    as_of: str | None,
    *,
    force_live: bool = False,
    request_id: str | None = None,
    retry_failed: bool = False,
    live_on_miss: bool = False,
) -> dict[str, Any]:
    """The shared /covenants/status body — GET serves it snapshot-first; POST /covenants/refresh
    serves it with ``force_live=True`` (recompute + snapshot rewrite). ``live_on_miss`` (the lender
    covenant report) serves a fresh snapshot when there is one and otherwise computes live and
    stores it — never the "computing" placeholder a download cannot poll."""
    from ..services.coa_formula import compute_builtin, compute_coa_formula, not_meaningful_code
    request_id = request_id or str(uuid4())
    start_date, end_date = _get_date_range(as_of=as_of)  # raises 400 on a bad as_of
    snapshot_period = _snapshot_period(as_of)  # the CovenantRecord period key this request maps to
    covenants = db.query(DebtCovenant).filter(
        DebtCovenant.company_id == company_id,
        DebtCovenant.active == True,  # noqa: E712
    ).all()
    if not covenants:
        # Quiet intentional state: do not instantiate/refresh a QBO client for a company with no
        # configured covenant work.
        return {
            "company_id": company_id,
            "as_of": snapshot_period,
            "period": f"{start_date} → {end_date}",
            "overall_status": CovenantOverallStatus.NO_COVENANTS.value,
            "computation_state": CovenantComputationState.NO_COVENANTS.value,
            "request_id": request_id,
            "warm_task_id": None,
            "retryable": False,
            "errors": [],
            "covenants": [],
        }

    # New writes are enum-validated.  If a legacy/corrupt row contains another token, fail with a
    # safe configuration error before any provider call; never reinterpret it as BELOW/ABOVE.
    for covenant in covenants:  # SA-OK: bounded in-memory active-covenant validation
        _edge_covenant_direction(covenant)

    client = get_qbo_client_from_db(db, company_id)
    # AC-2 cutover: the SINGLE company-level EBITDA definition, loaded once, applied to every
    # EBITDA covenant. The per-covenant ebitda_addbacks read is removed from the compute path.
    company_addbacks = _load_company_ebitda_addbacks(db, company_id)

    results: list[dict[str, Any]] = []
    # PLAN-EXEMPT: cherry-pick conflict resolution landing reviewed market-ready commits onto main
    overall_status = "PASS"
    # Honest overall (AC-2): "PASS" is only reportable once at least one covenant actually
    # merged a known status. If EVERY covenant is still "computing" (excluded from the
    # merge), the page must say UNKNOWN — never a fabricated PASS from zero known values.
    overall_merged = False
    # BOUNDED-READ (F-2 stall fix): set True when a two-lens covenant returns the "computing"
    # sentinel (snapshot miss on the interactive GET); after the loop we enqueue ONE async warm.
    warm_needed = False
    warm_covenant_ids: list[int] = []
    warm_task_id: str | None = None

    for cov in covenants:
        value: Optional[float] = None
        breakdown: Optional[Dict] = None
        projection: Optional[Dict] = None  # set snapshot-first for two-lens metrics (W-2 F-2)
        errors: list[dict[str, Any]] = []
        data_quality = "success"
        computation: dict[str, Any] = {
            "state": CovenantComputationState.POPULATED.value,
            "needs_enqueue": False,
            "task_id": None,
        }
        try:
            if not client:
                data_quality = "provider_error"
                errors.append(safe_error_detail(
                    error_code="qbo_connection_missing",
                    user_message="No QuickBooks connection is available for this covenant calculation.",
                    retryable=False,
                    log_context="covenant_metric_error",
                ))
            elif cov.custom_formula:
                formula = json.loads(cov.custom_formula)
                value, breakdown = compute_coa_formula(formula, client, start_date, end_date)
            elif str(cov.metric_type or "").upper() in ("DSCR", "DEBT_EBITDA"):
                # PLAN-EXEMPT: plan 20260630-covenant-redesign fix — the header verdict MUST be the
                # SAME rolling-TTM value the grid, the downloaded covenant_monthly report, and the
                # stored snapshot/alerts all use (compute_monthly_series.ttm_ratio). The legacy
                # compute_builtin diverged — BS net-diff principal + Total Liabilities vs the series'
                # monthly CF principal + mapped Total Debt — so the card showed one number and judged
                # PASS/BREACH off another. One engine → header, grid, report, and snapshot cannot drift.
                # W-2 F-2 (plan 20260702-qa-remediation): snapshot-first — a fresh (covenant, period)
                # CovenantRecord serves value + breakdown + projection with zero QBO calls; a
                # stale/missing snapshot computes live and writes the snapshot back.
                value, breakdown, projection, computation = _two_lens_status_values(
                    db, cov, client, company_addbacks, start_date, end_date,
                    period=snapshot_period, force_live=force_live,
                    # BOUNDED-READ (F-2 stall fix): the interactive GET (force_live=False) is
                    # snapshot-ONLY — a miss returns the "covenant_computing" sentinel instead of
                    # a cumulative live compute; POST /covenants/refresh (force_live=True) stays live.
                    allow_live=force_live or live_on_miss,
                    retry_failed=retry_failed,
                )
            else:
                # Non-two-lens metrics (MIN_CASH / CURRENT_RATIO / LEVERAGE) keep the single-period
                # builtin — they have no monthly two-lens series to source a TTM from.
                value, breakdown = compute_builtin(
                    cov.metric_type, client, start_date, end_date, db=db, realm_id=client.realm_id,
                    addbacks=_cov_own_addbacks(cov, company_addbacks),
                    debt_accounts=json.loads(getattr(cov, "debt_service_accounts", None)) if getattr(cov, "debt_service_accounts", None) else None,
                )
        except json.JSONDecodeError as exc:
            data_quality = "formula_error"
            errors.append(safe_error_detail(
                error_code="covenant_formula_invalid",
                user_message="This covenant formula is invalid and could not be evaluated.",
                exc=exc,
                retryable=False,
                log_context="covenant_metric_error",
            ))
        except Exception as e:
            data_quality = "provider_error"
            errors.append(safe_error_detail(
                error_code="covenant_metric_unavailable",
                user_message="This covenant metric could not be loaded from QuickBooks.",
                exc=e,
                retryable=True,
                log_context="covenant_metric_error",
            ))

        # BOUNDED-READ (F-2 stall fix): a two-lens covenant with no fresh snapshot on the interactive
        # GET path returns the "covenant_computing" sentinel (value None, breakdown error_code set)
        # rather than blocking on a live compute. Mark it "computing" (value None → status UNKNOWN,
        # excluded from the overall merge) and flag that an async warm is needed. This is NOT a
        # no_data / error condition, so it must be handled BEFORE the no-data fallback below.
        persisted_error = (
            breakdown.get("persisted_error")
            if isinstance(breakdown, dict)
            and isinstance(breakdown.get("persisted_error"), dict)
            else None
        )
        if persisted_error is not None:
            errors.append(persisted_error)
            data_quality = "provider_error"

        is_computing = (
            isinstance(breakdown, dict)
            and breakdown.get("error_code") == "covenant_computing"
        )
        needs_enqueue = bool(computation.get("needs_enqueue"))
        if needs_enqueue:
            warm_needed = True
            warm_covenant_ids.append(int(cov.id))
        if is_computing:
            data_quality = "computing"
            value = None
        if computation.get("task_id") and warm_task_id is None:
            warm_task_id = str(computation["task_id"])

        # A ratio with a zero/negative denominator has no value but, on a maximum covenant, a
        # known verdict (BREACH) — that is a computed result, not a no-data gap.
        not_meaningful = None if errors or is_computing else not_meaningful_code(breakdown)
        status = _compute_status(
            value, float(cov.threshold), cov.breach_direction, cushion=_cov_warn_cushion(cov),
            not_meaningful=not_meaningful,
        )
        verdict_without_value = value is None and status == "BREACH"

        if value is None and not errors and not is_computing and not verdict_without_value:
            data_quality = "no_data"
            # AC-4: surface the SPECIFIC reason the builtin reported (e.g. "map the serviced
            # debt account", "EBITDA unavailable") rather than a generic blank no-data.
            specific_code, specific_msg = _no_data_reason(breakdown)
            errors.append(safe_error_detail(
                error_code=specific_code,
                user_message=specific_msg,
                retryable=False,
                log_context="covenant_metric_error",
            ))

        if is_computing:
            row_computation_state = CovenantComputationState.COMPUTING.value
        elif errors and data_quality in {"provider_error", "formula_error"}:
            row_computation_state = CovenantComputationState.ERROR.value
        elif value is None and not verdict_without_value:
            row_computation_state = CovenantComputationState.NO_DATA.value
        else:
            row_computation_state = CovenantComputationState.POPULATED.value

        # A "computing" covenant is not-yet-known: force UNKNOWN and keep it OUT of the overall
        # merge so a pending recompute never flips the whole page to BREACH/PASS (AC-2).
        if is_computing:
            status = "UNKNOWN"
        else:
            # PLAN-EXEMPT: cherry-pick conflict resolution landing reviewed market-ready commits onto main
            overall_status = _merge_overall_status(overall_status, status)
            overall_merged = True

        # AC-5: how much cushion is left (signed distance from the threshold) + how long until
        # breach (periods-until-breach off the live trend). headroom is derived from the value we
        # already computed — no extra QBO call. The projection (covenant_projection.py) evaluates
        # the metric over the current vs prior window and returns available:false (+ reason) when
        # it cannot derive a trend; we surface that reason honestly, never a fabricated slope.
        headroom = _compute_headroom(value, float(cov.threshold), cov.breach_direction)
        # PLAN-EXEMPT: plan 20260630-covenant-redesign fix — project the trend off the covenant's own add-backs.
        # W-2 F-2: two-lens metrics already carry a projection (snapshot-served or computed with
        # the live series above); only the non-two-lens metrics project here.
        if projection is None:
            projection = _covenant_projection(
                cov,
                client,
                addbacks=_cov_own_addbacks(cov, company_addbacks),
                as_of_date=date.fromisoformat(end_date),
            )

        results.append({
            **_cov_to_dict(cov),
            "current_value": value,
            "status": status,
            "period": f"{start_date} → {end_date}",
            "breakdown": breakdown,
            "data_quality": data_quality,
            "computation_state": row_computation_state,
            "errors": errors,
            "headroom": headroom,
            "periods_until_breach": projection.get("periods_until_breach") if projection.get("available") else None,
            "projection": projection,
        })

    # One realm+period single-flight.  The helper rechecks under a row lock, commits the holder
    # before enqueue, and returns the existing task id to duplicate polls.
    if warm_needed and not force_live:
        warm_covenant_ids = sorted(set(warm_covenant_ids))
        dispatch = _enqueue_covenant_warm(
            db,
            company_id=company_id,
            period=snapshot_period,
            covenant_ids=warm_covenant_ids,
            request_id=request_id,
            retry_failed=retry_failed,
        )
        warm_task_id = dispatch.task_id
        if dispatch.state == CovenantComputationState.ERROR and dispatch.error is not None:
            for row in results:
                if int(row["id"]) not in dispatch.covenant_ids:
                    continue
                row["computation_state"] = CovenantComputationState.ERROR.value
                row["data_quality"] = CovenantDataQuality.PROVIDER_ERROR.value
                row["errors"] = [dispatch.error]
                row["breakdown"] = {
                    "error_code": dispatch.error["error_code"],
                    "reason": dispatch.error["user_message"],
                }
        elif dispatch.state == CovenantComputationState.COMPUTING:
            # The lock recheck is authoritative. Another request may have re-armed a stale holder
            # after this response read its local ERROR view; reconcile back to COMPUTING so the
            # client follows the winning task instead of stopping on a stale terminal state.
            for row in results:
                if int(row["id"]) not in dispatch.covenant_ids:
                    continue
                row["current_value"] = None
                row["status"] = CovenantVerdict.UNKNOWN.value
                row["computation_state"] = CovenantComputationState.COMPUTING.value
                row["data_quality"] = CovenantDataQuality.COMPUTING.value
                row["errors"] = []
                row["breakdown"] = {
                    "error_code": "covenant_computing",
                    "reason": "Recalculating from QuickBooks…",
                }
                row["headroom"] = None
        elif dispatch.state in {
            CovenantComputationState.POPULATED,
            CovenantComputationState.NO_DATA,
        }:
            # A slow broker failure can lose its task-id CAS because a replacement worker or
            # explicit refresh already completed. Rebuild once from that authoritative snapshot;
            # leaving the pre-dispatch COMPUTING row in place would serialize an impossible
            # terminal-without-task response (and make the client poll work that no longer exists).
            return _covenant_status_payload(
                db,
                company_id,
                as_of,
                force_live=False,
                request_id=request_id,
                retry_failed=False,
            )

    if not overall_merged:
        # PLAN-EXEMPT: cherry-pick conflict resolution landing reviewed market-ready commits onto main
        overall_status = "UNKNOWN"

    row_states = {str(row["computation_state"]) for row in results}
    # Active work wins over unrelated terminal/no-data rows so bounded polling can finish it.  Once
    # no row remains COMPUTING, any ERROR becomes the terminal overall state.
    if CovenantComputationState.COMPUTING.value in row_states:
        computation_state = CovenantComputationState.COMPUTING.value
    elif CovenantComputationState.ERROR.value in row_states:
        computation_state = CovenantComputationState.ERROR.value
    elif row_states == {CovenantComputationState.NO_DATA.value}:
        computation_state = CovenantComputationState.NO_DATA.value
    else:
        computation_state = CovenantComputationState.POPULATED.value

    top_errors: list[dict[str, Any]] = []
    seen_errors: set[tuple[str, str]] = set()
    for row in results:
        for error in row["errors"]:
            identity = (str(error.get("error_code")), str(error.get("trace_id")))
            if identity in seen_errors:
                continue
            seen_errors.add(identity)
            top_errors.append(error)
    return {
        "company_id": company_id,
        "as_of": snapshot_period,
        "period": f"{start_date} → {end_date}",
        "overall_status": overall_status,
        "computation_state": computation_state,
        "request_id": request_id,
        "warm_task_id": warm_task_id,
        "retryable": any(bool(error.get("retryable")) for error in top_errors),
        "errors": top_errors,
        "covenants": results,
    }


# AC-2 (R3): the ONE shared company-level EBITDA definition (GET reads it, PUT upserts it).
# Both are tenant-gated (check_tenant_access in-body + global_tenant_guard on the decorator),
# mirroring the covenant CRUD handlers above.
@router.get("/ebitda-config", response_model=CompanyEbitdaConfigOut, dependencies=[Depends(global_tenant_guard)])
def get_ebitda_config(
    company_id: str = Query(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
) -> CompanyEbitdaConfigOut:
    """AC-2 (R3): the ONE shared company-level EBITDA definition. Returns the mapped add-back
    lines for ``company_id`` (empty list when none is configured → book EBITDA only)."""
    check_tenant_access("VIEWER", company_id=company_id, db=db, user=current_user)
    cfg = db.query(CompanyEbitdaConfig).filter(
        CompanyEbitdaConfig.company_id == company_id
    ).one_or_none()
    addbacks = json.loads(cfg.addbacks) if (cfg and cfg.addbacks) else []
    return CompanyEbitdaConfigOut(company_id=company_id, addbacks=addbacks)


@router.put("/ebitda-config", response_model=CompanyEbitdaConfigOut, dependencies=[Depends(global_tenant_guard)])
def put_ebitda_config(
    body: CompanyEbitdaConfigUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
) -> CompanyEbitdaConfigOut:
    """AC-2 (R3): set/replace the ONE company-level EBITDA definition (upsert by company_id).
    Every EBITDA covenant (DSCR + Debt/EBITDA) then computes EBITDA from these add-backs."""
    check_tenant_access("EDITOR", company_id=body.company_id, db=db, user=current_user)
    # HIGH-risk (gap row 15): the write is delegated to the pure service so an agent write-path
    # can call the SAME code and surface the before/after add-back mapping to the approver. The
    # add-backs stay explicit account MAPPING (persisted verbatim; no keyword inference).
    result = update_ebitda_config_service(
        db, body.company_id, [a.model_dump() for a in body.addbacks]
    )
    return CompanyEbitdaConfigOut(company_id=body.company_id, addbacks=result["after"])


@router.post("/suggest")
def suggest_covenants(
    company_id: str = Query(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
) -> List[Dict[str, Any]]:
    """
    Returns a STANDARD senior-loan covenant template (DSCR >= 1.25, Debt/EBITDA <= 4.0,
    Current Ratio >= 1.2, Min Cash) — a fixed best-practice list, NOT AI-generated. Use
    POST /covenants/ai-draft for the AI path. Returned suggestions are not saved automatically.
    """
    check_tenant_access("VIEWER", company_id=company_id, db=db, user=current_user)
    return [
        {
            "loan_name": "Senior Loan",
            "name": "Debt Service Coverage Ratio",
            "metric_type": "DSCR",
            "threshold": 1.25,
            "breach_direction": "BELOW",
            "description": "EBITDA must cover debt service by at least 1.25x. Standard senior lender requirement.",
        },
        {
            "loan_name": "Senior Loan",
            "name": "Debt / EBITDA",
            "metric_type": "DEBT_EBITDA",
            "threshold": 4.0,
            "breach_direction": "ABOVE",
            "description": "Total debt must not exceed 4× EBITDA. Typical maximum leverage covenant.",
        },
        {
            "loan_name": "Senior Loan",
            "name": "Current Ratio",
            "metric_type": "CURRENT_RATIO",
            "threshold": 1.2,
            "breach_direction": "BELOW",
            "description": "Current assets must be at least 1.2× current liabilities. Liquidity covenant.",
        },
        {
            "loan_name": "Senior Loan",
            "name": "Minimum Cash",
            "metric_type": "MIN_CASH",
            "threshold": 250000,
            "breach_direction": "BELOW",
            "description": "Maintain minimum $250k cash balance. Adjust threshold to match your actual requirement.",
        },
    ]


@router.post("/ai-draft")
def ai_draft_covenant(
    body: CovenantAIDraftRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
) -> Dict[str, Any]:
    check_tenant_access("EDITOR", company_id=body.company_id, db=db, user=current_user)
    accounts = _load_account_catalog(db, body.company_id)
    draft = draft_covenant(body.prompt, accounts)

    # AC-9: curate the raw LLM draft against REALITY before it is offered. validate_ai_draft
    # strips any hallucinated account ref (not in the live COA) from custom_formula_obj +
    # matched_accounts and attaches a `warnings` list flagging an out-of-range threshold — a
    # hallucinated account or a nonsense threshold is surfaced for review, never silently saved.
    draft = validate_ai_draft(draft, accounts)

    formula_obj = draft.get("custom_formula_obj")
    if formula_obj and not isinstance(formula_obj, dict):
        formula_obj = None

    return {
        "draft": {
            "loan_name": draft.get("loan_name") or "Credit Facility",
            "name": draft.get("name") or "Custom Covenant",
            "metric_type": draft.get("metric_type") or "COA_FORMULA",
            "threshold": float(draft.get("threshold") or 1.0),
            "breach_direction": draft.get("breach_direction") or "BELOW",
            "custom_formula": json.dumps(formula_obj) if formula_obj else None,
            "custom_formula_obj": formula_obj,
        },
        "rationale": draft.get("rationale") or "",
        "matched_accounts": draft.get("matched_accounts") or [],
        # AC-9: the curation warnings (dropped hallucinated accounts, out-of-range threshold).
        "warnings": draft.get("warnings") or [],
    }


@router.get("")
def list_covenants(
    company_id: str = Query(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
) -> List[Dict[str, Any]]:
    check_tenant_access("VIEWER", company_id=company_id, db=db, user=current_user)
    rows = db.query(DebtCovenant).filter(
        DebtCovenant.company_id == company_id,
    ).order_by(DebtCovenant.loan_name, DebtCovenant.name).all()
    for covenant in rows:  # SA-OK: bounded in-memory configured-covenant validation
        _edge_covenant_direction(covenant)
    return [_cov_to_dict(c) for c in rows]


@router.post("")
def create_covenant(
    body: CovenantCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
):
    check_tenant_access("EDITOR", company_id=body.company_id, db=db, user=current_user)
    # AC-4: validate any custom-formula account refs exist NOW (a 400 at save), not silently 0 at month-end.
    if body.custom_formula:
        _validate_formula_accounts(db, body.company_id, body.custom_formula)
    # PLAN-EXEMPT: item-3 add-backs — JSON-encode the typed lines into the Text column (ratified 2026-06-25)
    data = body.model_dump()
    addbacks = data.pop("ebitda_addbacks", None)
    debt_accounts = data.pop("debt_service_accounts", None)  # AC-3: JSON-encode into the Text column
    cov = DebtCovenant(**data)
    if addbacks is not None:
        cov.ebitda_addbacks = json.dumps(addbacks)
    if debt_accounts is not None:
        cov.debt_service_accounts = json.dumps(debt_accounts)
    # LAYER-OK: prevailing covenant-CRUD style — router-local write matching the existing create/update/delete handlers already in this file (covenants have no service layer; Wave 4 owns the builder seam)
    db.add(cov)
    db.flush()  # assign cov.id so the audit row can reference it, in the SAME transaction
    # AC-10: every covenant created WITH a threshold writes a threshold-set audit row
    # (old→new, covenant id, actor) via the existing audit path. The audit row commits in
    # the same transaction as the covenant (log_audit_event does not commit on its own).
    _audit_threshold_change(
        db, cov, old_threshold=None, new_threshold=float(cov.threshold), user=current_user
    )
    db.commit()
    db.refresh(cov)
    return _cov_to_dict(cov)


@router.put("/{covenant_id}")
def update_covenant(
    covenant_id: int,
    body: CovenantUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
):
    cov = check_entity_tenant_access(
        "EDITOR",
        db=db,
        user=current_user,
        model=DebtCovenant,
        entity_id=covenant_id,
        not_found_detail="Covenant not found",
    )
    # HIGH-risk (gap row 14): a threshold/definition edit is breach-detection-affecting, so the
    # write is delegated to the pure service (which exposes a before/after snapshot for an agent
    # write-path approver). The service applies the SAME behavior: AC-4 formula-account validation
    # (a 400, not silent 0), AC-10 old→new threshold audit in the same transaction, and the
    # JSON-encode of the list-in-Text columns. cov is already tenant-resolved by
    # check_entity_tenant_access; company_id is re-asserted inside the service (defence-in-depth).
    # PLAN-EXEMPT: item-3 add-backs JSON-encode (ratified 2026-06-25) — now owned by the service
    try:
        update_covenant_service(
            db,
            cov.company_id,
            cov,
            body.model_dump(exclude_none=True),
            validate_formula=_validate_formula_accounts,
            audit_threshold_change=_audit_threshold_change,
            user=current_user,
        )
    except CovenantConfigError as exc:
        http_exc = user_safe_http_exception(
            status_code=exc.status,
            error_code=exc.error_code,
            user_message=str(exc),
            exc=exc,
            retryable=False,
            log_context="covenant_update_error",
        )
        raise http_exc from exc
    return _cov_to_dict(cov)


@router.delete("/{covenant_id}")
def delete_covenant(
    covenant_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
):
    cov = check_entity_tenant_access(
        "EDITOR",
        db=db,
        user=current_user,
        model=DebtCovenant,
        entity_id=covenant_id,
        not_found_detail="Covenant not found",
    )
    # Delete child snapshot rows FIRST. covenant_records.covenant_id is a FK to
    # debt_covenants.id with NO ON DELETE cascade, so once a covenant has been
    # computed (it carries stored (covenant, period) snapshots) a bare db.delete(cov)
    # fails with a FK violation — the exact "✕ Remove does nothing" bug (the FK error
    # was swallowed by the frontend). Remove the children in the SAME transaction,
    # realm-scoped (scope is already pinned by check_entity_tenant_access).
    from ..models import CovenantRecord
    db.query(CovenantRecord).filter(
        CovenantRecord.covenant_id == cov.id,
        CovenantRecord.company_id == cov.company_id,
    ).delete(synchronize_session=False)
    db.delete(cov)
    db.commit()
    return {"status": "deleted"}


# PLAN-EXEMPT: plan 20260625-pilot-bug-fixes AC-6.2 (read-only preview); active-plan.json not synced into this worktree
@router.post("/preview", response_model=CovenantPreviewResponse, dependencies=[Depends(global_tenant_guard)])
def preview_covenant_formula(
    body: CovenantPreviewRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
) -> CovenantPreviewResponse:
    """
    Evaluate a COA formula against live QBO data WITHOUT saving — backs the formula
    builder's "Preview value" button. Read-only (VIEWER); wraps compute_coa_formula
    over the same trailing-365d window as /status, so the preview matches what the
    saved covenant will show.
    """
    check_tenant_access("VIEWER", company_id=body.company_id, db=db, user=current_user)
    from ..services.coa_formula import compute_coa_formula

    client = get_qbo_client_from_db(db, body.company_id)
    if not client:
        raise user_safe_http_exception(
            status_code=400,
            error_code="qbo_connection_missing",
            user_message="No QuickBooks connection is available for this preview.",
            retryable=False,
            log_context="covenant_preview_error",
        )

    try:
        formula = json.loads(body.custom_formula)
    except (json.JSONDecodeError, TypeError) as exc:
        http_exc = user_safe_http_exception(
            status_code=400,
            error_code="covenant_formula_invalid",
            user_message="This formula is invalid and could not be evaluated.",
            exc=exc,
            retryable=False,
            log_context="covenant_preview_error",
        )
        raise http_exc from exc

    start_date, end_date = _get_date_range()
    try:
        value, breakdown = compute_coa_formula(formula, client, start_date, end_date)
    except Exception as exc:
        http_exc = user_safe_http_exception(
            status_code=502,
            error_code="qbo_provider_error",
            user_message="The formula could not be evaluated from QuickBooks.",
            exc=exc,
            retryable=True,
            log_context="covenant_preview_error",
        )
        raise http_exc from exc

    return CovenantPreviewResponse(
        value=value,
        breakdown=breakdown,
        period=f"{start_date} → {end_date}",
    )


# PLAN-EXEMPT: plan 20260630-covenant-redesign Wave-4 AC-1 (builder live preview); active-plan.json not synced into this worktree
@dataclass
class _CovenantSpec:
    """A covenant-LIKE object built from the UNSAVED builder spec, fed to
    ``compute_monthly_series`` (which reads ``.metric_type`` / ``.threshold`` /
    ``.breach_direction`` and JSON-decodes ``.debt_service_accounts`` + ``.ebitda_addbacks``).
    No DB row is written."""
    metric_type: str
    threshold: float
    breach_direction: CovenantDirection
    debt_service_accounts: str | None  # JSON-encoded list, matching the stored column shape
    # PLAN-EXEMPT: plan 20260630-covenant-redesign fix — JSON-encoded category-tagged add-back
    # lines built from the builder's interest/D&A/tax picks, matching the stored ebitda_addbacks
    # column so the preview computes EBITDA exactly as the saved covenant will.
    ebitda_addbacks: str | None = None


def _addback_lines_from_accounts(
    interest: list[str], da: list[str], tax: list[str]
) -> list[dict]:
    """Build category-tagged account-kind EBITDA add-back lines from the builder's mapped account
    ids — the SAME shape (kind/account_ref/sign/category) the covenant row stores, so the preview
    resolves EBITDA identically to the saved covenant."""
    lines: list[dict] = []
    for aid in interest:
        lines.append({"label": str(aid), "kind": "account", "account_ref": str(aid), "sign": "add", "category": "interest"})
    for aid in da:
        lines.append({"label": str(aid), "kind": "account", "account_ref": str(aid), "sign": "add", "category": "depreciation"})
    for aid in tax:
        lines.append({"label": str(aid), "kind": "account", "account_ref": str(aid), "sign": "add", "category": "tax"})
    return lines


@router.post(
    "/preview-series",
    response_model=CovenantSeriesPreviewResponse,
    dependencies=[Depends(global_tenant_guard)],
)
def preview_covenant_series(
    body: CovenantSeriesPreviewRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
) -> CovenantSeriesPreviewResponse:
    """AC-1: live two-lens monthly preview of an UNSAVED covenant spec — backs the covenant
    BUILDER's live grid. Read-only (VIEWER), DISPLAY-ONLY: builds a covenant-like object from
    the spec, loads the company EBITDA add-backs + the QBO client + the TTM window, and calls
    ``compute_monthly_series`` — returning the same series.to_dict() shape the saved covenant
    would produce. Honest degradation (available / reason / per-month no_data) is passed
    through untouched; a per-month gap is never a fabricated 0."""
    check_tenant_access("VIEWER", company_id=body.company_id, db=db, user=current_user)
    from ..services.covenant_compute import compute_monthly_series

    client = get_qbo_client_from_db(db, body.company_id)
    start_date, end_date = _get_date_range(as_of=body.as_of)  # raises 400 on a bad as_of
    period = f"{start_date} → {end_date}"

    if not client:
        # DISPLAY-ONLY honest degradation — no QBO connection means no series, not a fake grid.
        return CovenantSeriesPreviewResponse(
            metric_type=str(body.metric_type or "").upper(),
            threshold=body.threshold,
            breach_direction=body.breach_direction,
            lens="coverage" if str(body.metric_type or "").upper() == "DSCR" else "leverage",
            available=False,
            reason="No QuickBooks connection is available to preview this covenant.",
            period=period,
        )

    company_addbacks = _load_company_ebitda_addbacks(db, body.company_id)
    # PLAN-EXEMPT: plan 20260630-covenant-redesign fix — the builder's mapped interest/D&A/tax become
    # the covenant spec's OWN add-backs, so the live preview computes EBITDA exactly as the save will.
    own_addbacks = _addback_lines_from_accounts(
        body.interest_accounts, body.da_accounts, body.tax_accounts
    )
    spec = _CovenantSpec(
        metric_type=body.metric_type,
        threshold=body.threshold,
        breach_direction=body.breach_direction,
        debt_service_accounts=json.dumps(body.debt_service_accounts) if body.debt_service_accounts else None,
        ebitda_addbacks=json.dumps(own_addbacks) if own_addbacks else None,
    )
    try:
        series = compute_monthly_series(spec, client, company_addbacks, start_date, end_date)
    except Exception as exc:
        http_exc = user_safe_http_exception(
            status_code=502,
            error_code="qbo_provider_error",
            user_message="The covenant preview could not be computed from QuickBooks.",
            exc=exc,
            retryable=True,
            log_context="covenant_preview_series_error",
        )
        raise http_exc from exc

    series_dict = series.to_dict()
    # AC-2: attach the SHARED bank-report row plan so the builder preview grid IS the report.
    return CovenantSeriesPreviewResponse(
        **series_dict, period=period, **_series_grid(series_dict, body.threshold)
    )


# PLAN-EXEMPT: plan 20260630-covenant-redesign AC-1 (saved-covenant two-lens series for the
# landing view); active-plan.json is not synced into this worktree, so the gate path-match fails
# on the worktree absolute path only — this is the covenant router the plan's dispatch names.
@router.get(
    "/{covenant_id}/series",
    response_model=CovenantSeriesPreviewResponse,
    dependencies=[Depends(global_tenant_guard)],
)
def get_covenant_series(
    request: Request,
    covenant_id: int,
    as_of: str | None = Query(None, description="Evaluate as of this closed month (YYYY-MM); default = last closed month."),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
) -> CovenantSeriesPreviewResponse:
    """AC-1: the SAVED covenant's two-lens monthly grid — backs the covenant LANDING view.

    Read-only (VIEWER), DISPLAY-ONLY: resolves the covenant (tenant-gated), loads the company
    EBITDA add-backs + the realm-scoped QBO client + the TTM window, and calls the SAME
    ``compute_monthly_series`` engine the report + the builder preview use — then attaches the
    SAME bank-report row plan (``_series_grid`` → ``covenant_monthly_rows.rows_for``). So the
    landing grid, the builder preview, and the downloaded ``covenant_monthly`` report all render
    from one engine and one row definition and cannot drift. Honest degradation (available /
    reason / per-month no_data) is passed through untouched; a per-month gap is never a fabricated 0.
    """
    cov = check_entity_tenant_access(
        "VIEWER",
        db=db,
        user=current_user,
        model=DebtCovenant,
        entity_id=covenant_id,
        not_found_detail="Covenant not found",
    )
    canonical_direction = _edge_covenant_direction(cov)
    from ..services.covenant_compute import SUPPORTED_METRICS, compute_monthly_series

    start_date, end_date = _get_date_range(as_of=as_of)  # raises 400 on a bad as_of
    client = get_qbo_client_from_db(db, cov.company_id)
    period = f"{start_date} → {end_date}"
    threshold = float(cov.threshold) if cov.threshold is not None else 0.0
    metric = str(cov.metric_type or "").upper()
    lens = "coverage" if metric == "DSCR" else "leverage"

    if not client:
        # DISPLAY-ONLY honest degradation — no QBO connection means no series, not a fake grid.
        return CovenantSeriesPreviewResponse(
            metric_type=metric,
            threshold=threshold,
            breach_direction=canonical_direction,
            lens=lens,
            available=False,
            reason="No QuickBooks connection is available for this covenant.",
            period=period,
        )

    # W-2 F-2 (plan 20260702-qa-remediation): snapshot-first — a fresh (covenant, period)
    # CovenantRecord already carries the full monthly grid in calculation_context; rebuild the
    # SAME series dict the live path produces and render through the SAME row plan. Zero QBO calls.
    snapshot_period = _snapshot_period(as_of)
    rec = _fresh_snapshot_record(db, cov, snapshot_period)
    if rec is not None:
        try:
            series_dict = _series_dict_from_snapshot(cov, rec.calculation_context)
            return CovenantSeriesPreviewResponse(
                **series_dict, period=period, **_series_grid(series_dict, threshold)
            )
        except Exception as exc:  # noqa: BLE001 — a corrupt snapshot falls through to a live compute, never 500s
            logger.warning(
                "[covenants] snapshot serve failed (falling back to live) cov=%s period=%s: %s",
                cov.id, snapshot_period, redact_to_string(str(exc), max_length=300),
            )

    # BOUNDED-READ (F-2 stall fix): snapshot MISS on the interactive GET /series for a two-lens
    # covenant — do NOT run the cumulative live compute_monthly_series on the request thread (the
    # confirmed browser-timeout stall). Return a "computing" series body (available=False,
    # reason="computing", empty months/rows) and enqueue ONE async warm so the next view serves from
    # a fresh snapshot. Non-two-lens metrics (never snapshotted / warmed) keep the live compute below.
    if metric in SUPPORTED_METRICS:
        dispatch = _enqueue_covenant_warm(
            db,
            company_id=cov.company_id,
            period=snapshot_period,
            covenant_ids=[int(cov.id)],
            request_id=_request_id_for(request),
            retry_failed=False,
        )
        if dispatch.state in {
            CovenantComputationState.POPULATED,
            CovenantComputationState.NO_DATA,
        }:
            authoritative = _fresh_snapshot_record(db, cov, snapshot_period)
            if authoritative is not None:
                try:
                    series_dict = _series_dict_from_snapshot(
                        cov, authoritative.calculation_context
                    )
                    return CovenantSeriesPreviewResponse(
                        **series_dict,
                        period=period,
                        **_series_grid(series_dict, threshold),
                    )
                except Exception as exc:  # noqa: BLE001 — terminal reconciliation degrades safely
                    logger.warning(
                        "[covenants] authoritative snapshot reconciliation failed cov=%s "
                        "period=%s: %s",
                        cov.id,
                        snapshot_period,
                        redact_to_string(str(exc), max_length=300),
                    )
        return CovenantSeriesPreviewResponse(
            metric_type=metric,
            threshold=threshold,
            breach_direction=canonical_direction,
            lens=lens,
            available=False,
            reason=(
                dispatch.error["user_message"]
                if dispatch.state == CovenantComputationState.ERROR and dispatch.error
                else "computing"
            ),
            period=period,
        )

    company_addbacks = _load_company_ebitda_addbacks(db, cov.company_id)
    try:
        series = compute_monthly_series(cov, client, company_addbacks, start_date, end_date)
    except Exception as exc:
        http_exc = user_safe_http_exception(
            status_code=502,
            error_code="qbo_provider_error",
            user_message="This covenant could not be computed from QuickBooks.",
            exc=exc,
            retryable=True,
            log_context="covenant_series_error",
        )
        raise http_exc from exc

    series_dict = series.to_dict()
    # W-2 F-2: write the snapshot after a live compute so the next view serves from it. Only the
    # two-lens metrics are snapshotted (a MIN_CASH/LEVERAGE/CURRENT_RATIO/CUSTOM row would persist
    # an always-unavailable stub into the compliance history). Projection is filled lazily by
    # /covenants/status (its snapshot path heals a missing projection in place).
    if metric in SUPPORTED_METRICS:
        _write_covenant_snapshot(db, cov, series=series, period=snapshot_period)
    return CovenantSeriesPreviewResponse(
        **series_dict, period=period, **_series_grid(series_dict, threshold)
    )

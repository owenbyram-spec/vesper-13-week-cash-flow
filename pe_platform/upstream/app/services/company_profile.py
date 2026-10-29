# PLAN-EXEMPT: ratified plan 20260711-vesper_bot-finance-os FOS-1 Wave A — CompanyFinanceProfile accessor service
"""Company finance profile — the versioned policy accessor (FOS-1 Wave A).

``CompanyFinanceProfile`` rows are APPEND-ONLY versions of a company's financial
reporting policy. This module is the sanctioned read/write path:

- ``get_company_finance_profile`` resolves the effective policy VIEW for a
  tenant: the HIGHEST version whose ``effective_date`` is not in the future,
  merged over the fallback chain — ``QBOCompanyInfo.fiscal_year_start_month``
  for the fiscal-year start, the review_checks company materiality default for
  the floors, ``"accrual"`` for the basis — with per-field provenance so a
  caller can always tell WHERE a value came from
  (``"profile" | "qbo_company_info" | "default"``).
- ``upsert_company_finance_profile`` appends the NEXT version row (reads the
  current max version, +1) and stamps ``superseded_by_version`` on the prior
  latest row — the only in-place write versioning permits. Financial policy
  fields on existing rows are NEVER mutated.

Tenancy: callers pass the ALREADY-AUTHORIZED ``(organization_id, qbo_realm_id)``
resolved by the membership gate (``_check_company_access``) — never raw client
input (multi-tenancy-app-layer §4.3: derive every tenant filter from the gate's
resolved scope). Writes do NOT commit — the caller owns the transaction, so
rows land under the request's RLS GUC scope (the FORCE-RLS lesson).
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Literal, Mapping

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import REPORTING_BASES, CompanyFinanceProfile, QBOCompanyInfo
from .review_checks.materiality import Materiality

# ── Defaults (the fallback tier of the merge) ────────────────────────────────
# The company-level materiality default is the review_checks canonical
# company-shaped floor: the budget DUAL floor — ``Materiality.dual(pct=10,
# abs_floor=1000)`` (materiality.py:65; the proactive.py:764 shape, where BOTH
# the percent AND the absolute must clear). Built THROUGH the Materiality value
# object so the numbers stay pinned to the review_checks semantics rather than
# drifting as a local copy. Decimals throughout — never float
# (financial-modeling-correctness).
_DEFAULT_MATERIALITY = Materiality.dual(
    pct=Decimal("10"),
    abs_floor=Decimal("1000"),
    label="company default (review_checks budget DUAL floor)",
)
DEFAULT_MATERIALITY_ABS: Decimal = _DEFAULT_MATERIALITY.abs_floor
DEFAULT_MATERIALITY_PCT: Decimal = _DEFAULT_MATERIALITY.pct_floor
DEFAULT_REPORTING_BASIS = "accrual"
DEFAULT_FISCAL_YEAR_START_MONTH = 1  # January — calendar fiscal year

ProfileSource = Literal["profile", "qbo_company_info", "default"]

# Month-name → number (the finance_search.py:124 idiom). QBO's
# FiscalYearStartMonth is typically a month NAME string ("January", sync.py:106).
_MONTHS = {name.lower(): idx for idx, name in enumerate(calendar.month_name) if name}


@dataclass(frozen=True)
class FinanceProfileView:
    """The resolved, provenance-carrying finance policy for one tenant.

    ``provenance`` maps each policy field name to the tier that supplied its
    value: ``"profile"`` (a CompanyFinanceProfile row), ``"qbo_company_info"``
    (the QBO-synced company metadata), or ``"default"`` (the module defaults
    above). ``profile_id`` / ``profile_version`` / ``effective_date`` identify
    the resolved profile row; all three are None when no version is effective.
    """

    organization_id: int | None
    qbo_realm_id: str
    profile_id: int | None
    profile_version: int | None
    effective_date: date | None
    fiscal_year_start_month: int
    reporting_basis: str
    materiality_abs: Decimal
    materiality_pct: Decimal
    sign_conventions: dict | None
    kpi_preferences: dict | None
    close_cadence: dict | None
    notes: str | None
    provenance: Mapping[str, ProfileSource]


def _parse_month(value: object) -> int | None:
    """Coerce QBOCompanyInfo.fiscal_year_start_month to an int 1..12, or None.

    The column is a STRING from QBO sync — usually a month name ("January",
    sync.py:106-107) — but tolerate a numeric string or int. None on anything
    unparseable or out of range: the caller then falls through to the default,
    never a guess dressed as a value.
    """
    if value is None:
        return None
    if isinstance(value, int):
        return value if 1 <= value <= 12 else None
    text = str(value).strip()
    if not text:
        return None
    month = _MONTHS.get(text.lower())
    if month:
        return month
    try:
        num = int(text)
    except ValueError:
        return None
    return num if 1 <= num <= 12 else None


def _qbo_company_fiscal_month(db: Session, qbo_realm_id: str) -> int | None:
    """The QBO-synced fiscal-year start month for a realm, parsed; None if absent."""
    info = db.execute(
        select(QBOCompanyInfo).where(QBOCompanyInfo.realm_id == qbo_realm_id)
    ).scalars().first()
    if info is None:
        return None
    return _parse_month(info.fiscal_year_start_month)


def get_company_finance_profile(
    db: Session,
    organization_id: int | None,
    qbo_realm_id: str,
    *,
    as_of: date | None = None,
) -> FinanceProfileView:
    """Resolve the effective finance policy view for one tenant.

    Selection: the HIGHEST ``version`` whose ``effective_date <= as_of``
    (default today) — a future-dated version is ignored until its date arrives.
    Per-field merge, provenance-tagged:

      fiscal_year_start_month  profile → QBOCompanyInfo → default (January)
      reporting_basis          profile → default ("accrual")
      materiality_abs/_pct     profile → default (review_checks DUAL floor)
      sign_conventions / kpi_preferences / close_cadence / notes
                               profile → default (None)

    ``organization_id`` / ``qbo_realm_id`` MUST be the gate-resolved scope, not
    raw client input — both are applied as tenant filters.
    """
    effective_as_of = as_of or date.today()
    row = db.execute(
        select(CompanyFinanceProfile)
        .where(
            CompanyFinanceProfile.organization_id == organization_id,
            CompanyFinanceProfile.qbo_realm_id == qbo_realm_id,
            CompanyFinanceProfile.effective_date <= effective_as_of,
        )
        .order_by(CompanyFinanceProfile.version.desc())
        .limit(1)
    ).scalars().first()

    provenance: dict[str, ProfileSource] = {}

    # fiscal_year_start_month: profile → qbo_company_info → default
    fiscal_month = row.fiscal_year_start_month if row is not None else None
    if fiscal_month is not None:
        provenance["fiscal_year_start_month"] = "profile"
    else:
        qbo_month = _qbo_company_fiscal_month(db, qbo_realm_id)
        if qbo_month is not None:
            fiscal_month = qbo_month
            provenance["fiscal_year_start_month"] = "qbo_company_info"
        else:
            fiscal_month = DEFAULT_FISCAL_YEAR_START_MONTH
            provenance["fiscal_year_start_month"] = "default"

    # reporting_basis: profile → default
    if row is not None and row.reporting_basis:
        basis: str = row.reporting_basis
        provenance["reporting_basis"] = "profile"
    else:
        basis = DEFAULT_REPORTING_BASIS
        provenance["reporting_basis"] = "default"

    def _decimal_field(name: str, default: Decimal) -> Decimal:
        value = getattr(row, name) if row is not None else None
        if value is not None:
            provenance[name] = "profile"
            return Decimal(str(value))
        provenance[name] = "default"
        return default

    materiality_abs = _decimal_field("materiality_abs", DEFAULT_MATERIALITY_ABS)
    materiality_pct = _decimal_field("materiality_pct", DEFAULT_MATERIALITY_PCT)

    def _optional_field(name: str):
        value = getattr(row, name) if row is not None else None
        provenance[name] = "profile" if value is not None else "default"
        return value

    return FinanceProfileView(
        organization_id=organization_id,
        qbo_realm_id=qbo_realm_id,
        profile_id=row.id if row is not None else None,
        profile_version=row.version if row is not None else None,
        effective_date=row.effective_date if row is not None else None,
        fiscal_year_start_month=fiscal_month,
        reporting_basis=basis,
        materiality_abs=materiality_abs,
        materiality_pct=materiality_pct,
        sign_conventions=_optional_field("sign_conventions"),
        kpi_preferences=_optional_field("kpi_preferences"),
        close_cadence=_optional_field("close_cadence"),
        notes=_optional_field("notes"),
        provenance=provenance,
    )


def upsert_company_finance_profile(
    db: Session,
    *,
    organization_id: int | None,
    qbo_realm_id: str,
    organization_company_id: int | None = None,
    effective_date: date | None = None,
    fiscal_year_start_month: int | None = None,
    reporting_basis: str | None = None,
    materiality_abs: Decimal | None = None,
    materiality_pct: Decimal | None = None,
    sign_conventions: dict | None = None,
    kpi_preferences: dict | None = None,
    close_cadence: dict | None = None,
    notes: str | None = None,
) -> CompanyFinanceProfile:
    """Append the NEXT version row for ``(organization_id, qbo_realm_id)``.

    Append-only versioning: reads the realm's current ``max(version)``, writes
    ``version = max + 1``, and stamps ``superseded_by_version`` on the prior
    latest row — the supersession pointer is the ONLY in-place write allowed;
    financial policy fields on old versions are never touched. Note the pointer
    tracks version LINEAGE, not the effective window: a future-dated new version
    still supersedes lineage-wise, while reads keep resolving the old version
    until the new ``effective_date`` arrives.

    Validation is fail-loud BEFORE any write: an unknown ``reporting_basis``
    (only ``accrual|cash``) or an out-of-range ``fiscal_year_start_month``
    raises ValueError. Does NOT commit — the caller owns the transaction (rows
    must land under the request's RLS GUC scope). Concurrency: the
    ``(qbo_realm_id, version)`` UNIQUE constraint turns a double-append race
    into an IntegrityError for the loser instead of a silent duplicate.
    """
    basis = reporting_basis if reporting_basis is not None else DEFAULT_REPORTING_BASIS
    if basis not in REPORTING_BASES:
        raise ValueError(f"reporting_basis must be one of {REPORTING_BASES}, got {basis!r}")
    if fiscal_year_start_month is not None and not (1 <= int(fiscal_year_start_month) <= 12):
        raise ValueError(
            f"fiscal_year_start_month must be 1..12 (or None to defer to QBOCompanyInfo), "
            f"got {fiscal_year_start_month!r}"
        )

    current_max = db.execute(
        select(func.max(CompanyFinanceProfile.version)).where(
            CompanyFinanceProfile.qbo_realm_id == qbo_realm_id
        )
    ).scalar()
    next_version = (current_max or 0) + 1

    if current_max:
        prior = db.execute(
            select(CompanyFinanceProfile).where(
                CompanyFinanceProfile.qbo_realm_id == qbo_realm_id,
                CompanyFinanceProfile.version == current_max,
            )
        ).scalars().first()
        if prior is not None and prior.superseded_by_version is None:
            prior.superseded_by_version = next_version

    row = CompanyFinanceProfile(
        organization_id=organization_id,
        organization_company_id=organization_company_id,
        qbo_realm_id=qbo_realm_id,
        version=next_version,
        effective_date=effective_date or date.today(),
        fiscal_year_start_month=fiscal_year_start_month,
        reporting_basis=basis,
        materiality_abs=materiality_abs,
        materiality_pct=materiality_pct,
        sign_conventions=sign_conventions,
        kpi_preferences=kpi_preferences,
        close_cadence=close_cadence,
        notes=notes,
    )
    db.add(row)
    db.flush()
    return row

# PLAN-EXEMPT: ratified plan 20260713-wave2-5-cecl-allowance chunk A (marker
# RATIFICATION-2026-07-13.md on disk; active-plan.json not synced into this worktree).
"""
app/services/cecl_config.py
───────────────────────────
The CECL account-mapping CONFIG service (plan 20260713-wave2-5-cecl-allowance,
decision 1 — the covenant-config precedent, ``app/services/covenant_config.py``).

Owns the ``CompanyCeclConfig`` read + upsert: the EXPLICIT mapping of the
Allowance-for-Credit-Losses contra-asset and the Bad Debt expense account the
CECL engine drafts against. The mapping is NEVER inferred from account names —
the config route only SUGGESTS candidates; a human sets the mapping, and this
service validates each account's QBO ``Classification`` (Asset / Expense — not
loose type strings; allowances are variously typed AR or Other Current Asset in
the wild) against the live Account entity at SET time.

Layering (api-contract-and-layering): FAT service — takes ``db`` + ``company_id``
+ an injected ``fetch_account`` callable (the router passes the QBO client's
entity read so this module stays HTTP-unaware), owns the ``db.query``/``db.add``/
``db.commit``, returns plain dicts, and raises the domain ``CeclConfigError`` —
NEVER an HTTPException. QBO-transport failures raised by ``fetch_account``
(including the reconnect-required envelope) PROPAGATE untouched: the router maps
them, never this module.
"""
from __future__ import annotations

from collections.abc import Callable

from sqlalchemy.orm import Session

from ..models import CompanyCeclConfig

# QBO Classification values required per mapped account (decision 1 / AC-2):
# the allowance target must be a BALANCE-SHEET account, Bad Debt an EXPENSE —
# validated via Classification, not AccountType strings.
ALLOWANCE_REQUIRED_CLASSIFICATION = "Asset"
BAD_DEBT_REQUIRED_CLASSIFICATION = "Expense"


class CeclConfigError(Exception):
    """Domain error raised by the CECL config service.

    The router translates this into the one HTTP error envelope; the service
    stays FastAPI-unaware. ``status`` carries the intended HTTP status (400
    malformed input by default).
    """

    def __init__(self, message: str, *, error_code: str, status: int = 400) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.status = status


def _config_snapshot(cfg: CompanyCeclConfig | None, company_id: str) -> dict[str, str | bool | None]:
    """A plain-data snapshot of the mapping (the before/after payload)."""
    allowance_id = cfg.allowance_account_id if cfg is not None else None
    bad_debt_id = cfg.bad_debt_account_id if cfg is not None else None
    return {
        "company_id": company_id,
        "allowance_account_id": allowance_id,
        "bad_debt_account_id": bad_debt_id,
        "configured": bool(allowance_id and bad_debt_id),
    }


def get_cecl_config_service(db: Session, company_id: str) -> dict[str, str | bool | None]:
    """The company's CECL mapping snapshot (unset fields are None)."""
    cfg = (
        db.query(CompanyCeclConfig)
        .filter(CompanyCeclConfig.company_id == company_id)
        .one_or_none()
    )
    return _config_snapshot(cfg, company_id)


def _validate_account_classification(
    fetch_account: Callable[[str], dict],
    account_id: str,
    *,
    required_classification: str,
    role_label: str,
) -> None:
    """Assert the QBO Account entity carries the required Classification.

    ``fetch_account`` errors (network / reconnect-required) propagate untouched —
    the router owns the transport-error mapping. A missing entity or a wrong
    Classification raises the domain error.
    """
    resp = fetch_account(str(account_id))
    account = (resp or {}).get("Account") or {}
    if not account:
        raise CeclConfigError(
            f"QuickBooks returned no Account entity for {role_label} account {account_id} "
            "— refusing to save an unverifiable mapping.",
            error_code="cecl_config_account_not_found",
            status=400,
        )
    classification = str(account.get("Classification") or "").strip()
    if classification != required_classification:
        raise CeclConfigError(
            f"The {role_label} account {account_id} has QBO Classification "
            f"'{classification or 'unknown'}' — it must be '{required_classification}'. "
            "The mapping was not saved.",
            error_code="cecl_config_invalid_account_classification",
            status=400,
        )


def update_cecl_config_service(
    db: Session,
    company_id: str,
    *,
    allowance_account_id: str | None,
    bad_debt_account_id: str | None,
    fetch_account: Callable[[str], dict],
) -> dict[str, dict[str, str | bool | None] | str | bool]:
    """Set or unset the CECL account mapping (upsert by ``company_id``).

    Both accounts are REQUIRED-WHEN-SET (decision 1): both non-null = set (each
    Classification-validated via ``fetch_account`` before anything persists);
    both null = unset (no QBO call needed — nulls need no validation); exactly
    one null = refused. Returns ``{"before": ..., "after": ..., "company_id",
    "changed"}`` (the covenant-config shape).
    """
    allowance_id = (str(allowance_account_id).strip() or None) if allowance_account_id is not None else None
    bad_debt_id = (str(bad_debt_account_id).strip() or None) if bad_debt_account_id is not None else None

    if (allowance_id is None) != (bad_debt_id is None):
        raise CeclConfigError(
            "The CECL mapping requires BOTH the allowance account and the bad-debt "
            "account (or both null to unset) — a half-mapping cannot draft a "
            "balanced adjustment JE.",
            error_code="cecl_config_incomplete_mapping",
            status=400,
        )

    # Validate BEFORE any persistence — a failed validation must leave the row untouched.
    if allowance_id is not None and bad_debt_id is not None:
        _validate_account_classification(
            fetch_account,
            allowance_id,
            required_classification=ALLOWANCE_REQUIRED_CLASSIFICATION,
            role_label="allowance",
        )
        _validate_account_classification(
            fetch_account,
            bad_debt_id,
            required_classification=BAD_DEBT_REQUIRED_CLASSIFICATION,
            role_label="bad-debt expense",
        )

    cfg = (
        db.query(CompanyCeclConfig)
        .filter(CompanyCeclConfig.company_id == company_id)
        .one_or_none()
    )
    before = _config_snapshot(cfg, company_id)

    if cfg is None:
        cfg = CompanyCeclConfig(
            company_id=company_id,
            allowance_account_id=allowance_id,
            bad_debt_account_id=bad_debt_id,
        )
        db.add(cfg)
    else:
        cfg.allowance_account_id = allowance_id
        cfg.bad_debt_account_id = bad_debt_id

    db.commit()
    db.refresh(cfg)

    after = _config_snapshot(cfg, company_id)
    return {
        "before": before,
        "after": after,
        "company_id": company_id,
        "changed": before != after,
    }

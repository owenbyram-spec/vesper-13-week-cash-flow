"""
app/services/covenant_config.py
───────────────────────────────
The two covenant CONFIG WRITE paths, extracted as pure, callable services (plan
20260706-cfo-grade-financial-workflows, wave 3.2a — HIGH-risk gap rows 14/15).

Both are BEHAVIOR-PRESERVING extractions of what previously lived inline in
``app/routers/covenants.py``. They own NO covenant math — the covenant threshold
semantics and the EBITDA add-back MAPPING are unchanged; these functions only
persist the config the compute engine (``covenant_compute.py``) later reads. They
exist so an agent write-path can propose the change and surface an EXACT before/
after snapshot to the approver, because BOTH edits are breach-detection-affecting:

  • ``update_covenant_service``     — a threshold / definition edit changes what a
    later recompute flags as BREACH/WARN/PASS.
  • ``update_ebitda_config_service`` — an EBITDA add-back MAPPING edit changes the
    EBITDA the DSCR + Debt/EBITDA covenants compute from.

Layering (api-contract-and-layering): these are FAT services — they take ``db`` +
``company_id`` + explicit params/ORM in, own the ``db.query`` / ``db.add`` /
``db.commit``, return plain dicts, and raise a domain ``CovenantConfigError`` —
NEVER an HTTPException. The router maps that domain error to the one envelope.

Add-backs stay explicit account MAPPING, never keyword inference (the CFO-grade
rule): this module only serializes the caller-supplied add-back lines verbatim; it
never infers a category from an account name.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any  # CRAFT-OK: covenant patch values + JSON add-back lines are a heterogeneous, already-Pydantic-validated dynamic payload boundary (mirrors router _cov_to_dict -> dict[str, Any])

from sqlalchemy.orm import Session

from ..models import CompanyEbitdaConfig, DebtCovenant


class CovenantConfigError(Exception):
    """Domain error raised by the covenant-config write services.

    The router translates this into the one HTTP error envelope; the service layer
    stays FastAPI-unaware (raises this, never HTTPException). ``status`` carries the
    intended HTTP status the router should map to (400 malformed input by default).
    """

    def __init__(self, message: str, *, error_code: str, status: int = 400) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.status = status


def _addbacks_of(cov: DebtCovenant) -> list[Any]:  # CRAFT-OK: add-back lines are already-validated Pydantic model_dump dicts round-tripped through JSON — a dynamic payload boundary
    """The covenant's parsed ``ebitda_addbacks`` list (empty when unset/malformed)."""
    raw = getattr(cov, "ebitda_addbacks", None)
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    return parsed if isinstance(parsed, list) else []


def _debt_accounts_of(cov: DebtCovenant) -> list[Any]:  # CRAFT-OK: debt_service_accounts is a JSON list of opaque QBO account-ref strings/ids — a dynamic payload boundary
    """The covenant's parsed ``debt_service_accounts`` list (empty when unset/malformed)."""
    raw = getattr(cov, "debt_service_accounts", None)
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    return parsed if isinstance(parsed, list) else []


def _covenant_snapshot(cov: DebtCovenant) -> dict[str, Any]:  # CRAFT-OK: before/after snapshot is a heterogeneous JSON-serializable payload mirroring router _cov_to_dict -> dict[str, Any]
    """A plain-data snapshot of a covenant's breach-detection-affecting fields.

    Exposed as the before/after payload so the approver sees EXACTLY what a
    threshold/definition edit changes (threshold + breach_direction + the mapped
    EBITDA add-backs + debt-service accounts + warn cushion all feed the verdict).
    """
    return {
        "id": cov.id,
        "company_id": cov.company_id,
        "loan_name": cov.loan_name,
        "name": cov.name,
        "metric_type": cov.metric_type,
        "threshold": float(cov.threshold),
        "breach_direction": cov.breach_direction,
        "custom_formula": cov.custom_formula,
        "ebitda_addbacks": _addbacks_of(cov),
        "debt_service_accounts": _debt_accounts_of(cov),
        "warn_cushion": (
            float(cov.warn_cushion) if getattr(cov, "warn_cushion", None) is not None else None
        ),
        "active": cov.active,
    }


# JSON-encoded-into-Text list columns (mirrors the router's create/update encoding).
_JSON_LIST_FIELDS = ("ebitda_addbacks", "debt_service_accounts")


def update_covenant_service(
    db: Session,
    company_id: str,
    covenant: DebtCovenant,
    updates: dict[str, Any],  # CRAFT-OK: `updates` is a CovenantUpdate.model_dump — heterogeneous already-validated patch values (str/float/bool/list)
    *,
    validate_formula: Callable[[Session, str, str], None] | None = None,
    audit_threshold_change: Callable[..., None] | None = None,
    user: Any = None,  # CRAFT-OK: `user` is the opaque acting-principal ORM row, forwarded only to the injected audit hook
) -> dict[str, Any]:  # CRAFT-OK: returns the before/after snapshot dict — a heterogeneous JSON-serializable payload boundary
    """Apply a covenant threshold / definition edit, behavior-preserving (gap row 14).

    Args:
        db: the request/session-scoped Session (tenant scope already pinned by the
            router's ``check_entity_tenant_access``).
        company_id: the owning tenant/realm — asserted against ``covenant.company_id``
            (defence-in-depth; the covenant was already tenant-resolved by the router).
        covenant: the tenant-resolved ``DebtCovenant`` ORM row to patch.
        updates: the patch dict (a ``CovenantUpdate.model_dump(exclude_none=True)``).
        validate_formula: optional ``(db, company_id, custom_formula) -> None`` account-
            ref validator, injected so the service stays HTTP-unaware (the router passes
            its ``_validate_formula_accounts``). Called ONLY when a new custom_formula is
            supplied — identical to the prior inline order.
        audit_threshold_change: optional ``(db, cov, *, old_threshold, new_threshold,
            user) -> None`` audit hook (the router's ``_audit_threshold_change``), invoked
            in the SAME transaction only when the threshold actually changes.
        user: the acting principal, forwarded to the audit hook.

    Returns:
        ``{"before": {...}, "after": {...}, "threshold_changed": bool,
           "covenant_id": int}`` — before/after are full covenant snapshots so the
        approver sees the exact threshold + definition delta.

    Raises:
        CovenantConfigError: on a cross-tenant covenant or a covenant-config problem.
        (a formula-validation failure propagates from ``validate_formula``.)
    """
    if company_id != covenant.company_id:
        raise CovenantConfigError(
            "Covenant does not belong to this company.",
            error_code="covenant_company_mismatch",
            status=404,
        )

    before = _covenant_snapshot(covenant)

    # AC-4 parity: validate a newly-supplied custom formula's account refs at save.
    new_formula = updates.get("custom_formula")
    if new_formula and validate_formula is not None:
        validate_formula(db, covenant.company_id, new_formula)

    # Snapshot the threshold BEFORE the patch so a change can be audited old→new.
    old_threshold = float(covenant.threshold)
    for field, val in updates.items():
        if field in _JSON_LIST_FIELDS:
            val = json.dumps(val)
        setattr(covenant, field, val)

    new_threshold = float(covenant.threshold)
    threshold_changed = new_threshold != old_threshold
    if threshold_changed and audit_threshold_change is not None:
        audit_threshold_change(
            db,
            covenant,
            old_threshold=old_threshold,
            new_threshold=new_threshold,
            user=user,
        )

    db.commit()
    db.refresh(covenant)

    return {
        "before": before,
        "after": _covenant_snapshot(covenant),
        "threshold_changed": threshold_changed,
        "covenant_id": covenant.id,
    }


def update_ebitda_config_service(
    db: Session,
    company_id: str,
    addbacks: list[dict[str, Any]],  # CRAFT-OK: add-back lines are already-validated CovenantAddback.model_dump dicts — a dynamic JSON payload boundary
) -> dict[str, Any]:  # CRAFT-OK: returns the before/after add-back mapping payload — a heterogeneous JSON-serializable boundary
    """Set/replace the ONE company-level EBITDA add-back definition (gap row 15).

    Upserts ``CompanyEbitdaConfig`` for ``company_id`` — the single source of EBITDA
    add-backs every EBITDA covenant (DSCR + Debt/EBITDA) computes from. Add-backs are
    persisted as an explicit account MAPPING exactly as supplied; no keyword inference.

    Args:
        db: the request/session-scoped Session (tenant scope pinned by the router).
        company_id: the owning tenant/realm — the upsert key AND the query predicate.
        addbacks: the list of add-back line dicts (a ``CompanyEbitdaConfigUpdate``'s
            ``[a.model_dump() for a in body.addbacks]``) — persisted verbatim.

    Returns:
        ``{"before": [...], "after": [...], "company_id": str, "changed": bool}`` —
        before/after are the add-back mapping lists, so the approver sees exactly how
        the covenant EBITDA inputs change.

    Raises:
        CovenantConfigError: reserved for a config-shape problem (none currently raised
            — the caller supplies already-validated Pydantic lines).
    """
    cfg = (
        db.query(CompanyEbitdaConfig)
        .filter(CompanyEbitdaConfig.company_id == company_id)
        .one_or_none()
    )

    if cfg is None or not cfg.addbacks:
        before: list[Any] = []  # CRAFT-OK: prior add-back lines are opaque JSON dicts — a dynamic payload boundary
    else:
        try:
            parsed = json.loads(cfg.addbacks)
            before = parsed if isinstance(parsed, list) else []
        except (json.JSONDecodeError, TypeError):
            before = []

    encoded = json.dumps(addbacks)
    if cfg is None:
        cfg = CompanyEbitdaConfig(company_id=company_id, addbacks=encoded)
        db.add(cfg)
    else:
        cfg.addbacks = encoded

    db.commit()

    return {
        "before": before,
        "after": addbacks,
        "company_id": company_id,
        "changed": before != addbacks,
    }

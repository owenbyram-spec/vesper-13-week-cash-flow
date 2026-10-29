"""
app/lbo/assumption_proposals.py
───────────────────────────────
F-B4e Phase 3b — agent-PROPOSED LBO assumption defaults (a PURE heuristic blend) +
the tenant-scoped learning-loop read/write helpers over the live ``CompanyKnowledge``
store.

The proposal is a **heuristic blend, NOT an LLM**: for each assumption key it picks a
default with the precedence

    house (learned firm convention)  ›  historical (Phase-2 QBO derivation)  ›  schema (Phase-3 default)

and emits a ``{value, source, rationale, historical_anchor}`` record per key. It is
**ADVISORY only** (WS-4): ``blend_assumption_defaults`` is a PURE function — it reads no
DB, calls no LLM, and NEVER writes ``driver_data``. Nothing in this module applies a
proposal to the stored drivers; the model's numbers change ONLY when the user accepts via
``save_drivers``.

WS-5 (no fabricated anchor): a ``historical_anchor`` is present ONLY for the QBO-derivable
keys (dso / dpo / capex / tax). Sponsor-input keys (entry/exit multiple, hold year,
carry/sweep) have NO QBO derivation, so their anchor is ``None`` and the blend degrades to
house › schema — a made-up anchor for a multiple would be a fabricated number, which the
financial-modeling-correctness skill forbids.

WS-1 (tenant isolation): every ``CompanyKnowledge`` read/write in the DB helpers
(``learn_assumption_overrides`` / ``retrieve_house_assumptions``) is scoped
``company_id == realm`` AND ``category == ASSUMPTION_CATEGORY`` — an unscoped read leaks one
firm's deal conventions into another's proposal.

The pure ``blend_assumption_defaults`` imports only the stdlib + the Phase-3 schema, so the
golden runs conftest-free.
"""
from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from ..models import CompanyKnowledge

logger = logging.getLogger(__name__)

# The CompanyKnowledge category 3b owns. Isolates the structured assumption rows from the
# free-text "Deal Context" / user-teach rows so the defensive retrieve (WS-6) only parses
# its own JSON shape and the realm+category filter (WS-1) is exact.
ASSUMPTION_CATEGORY = "Deal Assumptions"
# The source tag stamped on the learn-hook's CompanyKnowledge rows.
ASSUMPTION_SOURCE = "lbo_assumption"

# The proposal source tags (the ``source`` field of each blended record).
SOURCE_HOUSE = "house"          # a learned firm convention (a prior user override)
SOURCE_QBO_DERIVED = "qbo_derived"  # a Phase-2 historical derivation (a QBO anchor)
SOURCE_TEMPLATE = "template"    # the Phase-3 schema default (no house, no anchor)

# The assumption keys that HAVE a QBO-derivable historical anchor (Phase-2
# seed_derivation): DSO, DPO, capex, effective-tax. ONLY these may carry a
# ``historical_anchor`` (WS-5). Every other key is a sponsor input with no QBO basis —
# including the Phase-2A (plan 20260706 AC-2A.1/AC-2A.2) additions: sweep_pct /
# sweep_pct_by_year / min_cash_floor (credit-agreement terms), management_rollover_amount,
# exit_ebitda_basis, and the goodwill_book/tax_amort_years split. They blend
# house › schema automatically (every LboAssumptions key is proposed) and NEVER get a
# fabricated anchor; min_cash_floor's runway-based QBO derivation is a future extension
# and is deliberately NOT anchored until it exists.
QBO_DERIVABLE_KEYS = frozenset({"ar_dso", "ap_dpo", "capex_pct", "tax_rate"})

# Human rationale fragments per source, keyed for the golden's substring assertions.
_RATIONALE_HOUSE = "learned firm convention from a prior accepted override"
_RATIONALE_QBO = "derived from this company's QuickBooks history"
_RATIONALE_TEMPLATE_QBO = "template default — no QuickBooks history available yet"
_RATIONALE_TEMPLATE_SPONSOR = "template default — no QuickBooks basis (a sponsor assumption)"


def blend_assumption_defaults(house: dict, historical: dict, schema: dict) -> dict:
    """Blend the three default sources into a per-key proposal record. PURE + READ-ONLY.

    Precedence per key: ``house`` (a learned value) › ``historical`` (a Phase-2 QBO anchor,
    only for the QBO-derivable keys) › ``schema`` (the Phase-3 default). Returns, per key in
    ``schema``::

        {"value": <chosen>, "source": "house"|"qbo_derived"|"template",
         "rationale": <human str>, "historical_anchor": <float|None>}

    WS-5: ``historical_anchor`` is non-None ONLY for ``QBO_DERIVABLE_KEYS`` and only when
    ``historical`` actually carries that key — a sponsor-input key NEVER gets a fabricated
    anchor. WS-4: this never writes ``driver_data``; it returns proposals for display.

    Args:
        house: ``{key: value}`` learned firm defaults (from the tenant-scoped retrieve).
        historical: ``{key: value}`` Phase-2 QBO derivations (anchors); QBO-keys only.
        schema: the Phase-3 ``LboAssumptions().model_dump()`` — the full default set + the
            iteration universe (every proposed key is a real schema key).

    Returns:
        ``{key: record}`` for every key in ``schema``.
    """
    proposals: dict = {}
    for key in schema:
        schema_default = schema[key]
        # WS-5: an anchor exists only for a QBO-derivable key that is actually present in
        # the historical map; never fabricate one for a sponsor input.
        anchor = None
        if key in QBO_DERIVABLE_KEYS and key in historical and historical[key] is not None:
            anchor = historical[key]

        if key in house and house[key] is not None:
            # House (learned) wins outright. The anchor (if any) rides along for display.
            proposals[key] = {
                "value": house[key],
                "source": SOURCE_HOUSE,
                "rationale": _RATIONALE_HOUSE,
                "historical_anchor": anchor,
            }
        elif anchor is not None:
            # No house value, but a QBO-derived anchor exists → propose the anchor.
            proposals[key] = {
                "value": anchor,
                "source": SOURCE_QBO_DERIVED,
                "rationale": _RATIONALE_QBO,
                "historical_anchor": anchor,
            }
        else:
            # Neither: fall back to the schema (template) default. The rationale records
            # WHY there is no anchor — a QBO-key with no history vs a sponsor input.
            rationale = (
                _RATIONALE_TEMPLATE_QBO
                if key in QBO_DERIVABLE_KEYS
                else _RATIONALE_TEMPLATE_SPONSOR
            )
            proposals[key] = {
                "value": schema_default,
                "source": SOURCE_TEMPLATE,
                "rationale": rationale,
                "historical_anchor": None,
            }
    return proposals


def compute_assumption_overrides(driver_data: dict, schema_default: dict) -> dict:
    """The assumptions the user OVERRODE: those in ``driver_data`` that DIFFER from the
    Phase-3 schema default (WS-2 — the stateless baseline). PURE.

    A value equal to the schema default is NOT a house signal and is excluded. Only keys
    that exist in ``schema_default`` are considered (an unknown extra key is not a known
    assumption). Returns ``{key: value}`` of the deliberate non-default choices.
    """
    overrides: dict = {}
    for key in schema_default:
        if key not in driver_data:
            continue
        if driver_data[key] != schema_default[key]:
            overrides[key] = driver_data[key]
    return overrides


def _parse_assumption_content(content: str) -> dict | None:
    """Defensively parse a CompanyKnowledge ``content`` JSON into the ``{key: value}``
    override map (WS-6). Returns ``None`` (skip the row) on any non-JSON / wrong-shape
    content — NEVER raises.

    The learn-hook stores ``{"assumptions": {key: value}, "summary": <str>}``; a legacy or
    malformed row is skipped rather than crashing the retrieve.
    """
    if not content:
        return None
    try:
        parsed = json.loads(content)
    except (ValueError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None
    assumptions = parsed.get("assumptions")
    if not isinstance(assumptions, dict):
        return None
    return assumptions


def retrieve_house_assumptions(db: Session, realm: str) -> dict:
    """Read the learned house assumption defaults for ONE realm from CompanyKnowledge.

    WS-1 (tenant isolation): the query filters ``CompanyKnowledge.company_id == realm`` AND
    ``CompanyKnowledge.category == ASSUMPTION_CATEGORY`` — NEVER a bare query. An unscoped
    read would surface another firm's deal conventions.

    WS-6: each row's ``content`` is parsed defensively (a non-JSON / malformed row is
    skipped, never raised). Most-recent wins per key: rows are read newest-first by ``id``
    and the FIRST value seen for a key is kept.

    Returns ``{key: value}`` of the learned house defaults (possibly empty).
    """
    from ..models import CompanyKnowledge

    rows = (
        db.query(CompanyKnowledge)
        .filter(
            CompanyKnowledge.company_id == realm,
            CompanyKnowledge.category == ASSUMPTION_CATEGORY,
        )
        .order_by(CompanyKnowledge.id.desc())
        .all()
    )
    house: dict = {}
    for row in rows:
        assumptions = _parse_assumption_content(row.content)
        if not assumptions:
            continue
        for key, value in assumptions.items():
            # Most-recent-wins: rows are newest-first, so do not overwrite an already-seen key.
            if key not in house:
                house[key] = value
    return house


def _build_assumption_content(overrides: dict) -> str:
    """The structured CompanyKnowledge ``content`` for the learn-hook: a JSON carrying the
    full current override-set + a human summary. PURE.

    The full override-set is stored (not a delta) so the most-recent-wins retrieve is
    correct without scanning history (WS-3).
    """
    keys = sorted(overrides)
    parts = ", ".join(f"{k}={overrides[k]}" for k in keys)
    summary = f"Firm assumption defaults (overridden from template): {parts}."
    return json.dumps({"assumptions": overrides, "summary": summary}, sort_keys=True)


def learn_assumption_overrides(
    db: Session, realm: str, driver_data: dict, schema_default: dict
) -> CompanyKnowledge | None:
    """Write the user's overridden assumptions to CompanyKnowledge — the learning hook.

    Computes the overrides (≠ the schema default, WS-2), and writes a CompanyKnowledge row
    ONLY when the override-set CHANGED vs the last write for this realm (WS-3 — no-op /
    identical saves do not append). The row is realm + org-FK scoped exactly like the
    lbo.py:2901 precedent (WS-1), with ``embedding_vector=None`` (a background worker
    computes it). Returns the created row, or ``None`` when nothing was written.

    Does NOT commit — the caller owns the transaction (the save_drivers commit also persists
    this row atomically with the driver_data).
    """
    from ..models import CompanyKnowledge
    from ..services.tenant_companies import (
        organization_company_for_realm,
        organization_for_company,
    )

    overrides = compute_assumption_overrides(driver_data, schema_default)
    if not overrides:
        # No deliberate non-default choices → no house signal to learn.
        return None

    # WS-3: skip the write when the current override-set matches the most-recent stored one.
    existing_house = retrieve_house_assumptions(db, realm)
    if existing_house == overrides:
        return None

    organization = organization_for_company(db, realm)
    organization_company = (
        organization_company_for_realm(db, organization_id=organization.id, realm_id=realm)
        if organization
        else None
    )

    knowledge = CompanyKnowledge(
        organization_id=organization.id if organization else None,
        organization_company_id=organization_company.id if organization_company else None,
        company_id=realm,
        category=ASSUMPTION_CATEGORY,
        content=_build_assumption_content(overrides),
        confidence="high",  # a deliberate user override is a high-confidence house signal
        source=ASSUMPTION_SOURCE,
        embedding_vector=None,  # computed by a background worker
    )
    db.add(knowledge)
    return knowledge


def propose_assumption_defaults(
    db: Session, realm: str, schema_default: dict, historical: dict | None = None
) -> dict:
    """Compose the tenant-scoped house retrieve + the Phase-2 anchors + the Phase-3 schema
    into the per-key proposal blend. READ-ONLY (WS-4) — returns proposals for display, never
    writes driver_data.

    Args:
        db: the request session.
        realm: the QBO realm (the tenant boundary — scopes the house retrieve, WS-1).
        schema_default: ``LboAssumptions().model_dump()`` (the Phase-3 defaults + key universe).
        historical: optional ``{key: value}`` Phase-2 QBO derivations (anchors). When absent,
            no anchors are proposed (the blend degrades to house › schema).
    """
    house = retrieve_house_assumptions(db, realm)
    return blend_assumption_defaults(house, historical or {}, schema_default)

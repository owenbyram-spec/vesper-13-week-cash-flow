"""app/services/pbc_binder.py — WF-9 PBC checklist + evidence-binder service (Wave 3.4).

Plan 20260714-wave3-4-tax-pbc-binder, AC-2. Two responsibilities in one module:

  * the PURE-READER evidence-binder ASSEMBLER (``assemble_binder`` / ``run_pbc_binder``)
    — for each PBC line it RE-RESOLVES the human-asserted evidence links UNDER THE
    TENANT SCOPE and reports ``LINKED`` (a real, same-tenant source row resolves) or
    ``NO_DATA`` (none does). It **NEVER** fabricates or infers a link, **NEVER** marks a
    line satisfied, and **NEVER** lets untrusted evidence content drive linking — the
    going-concern / CECL / tax-package assemble-not-judge precedent.
  * the human-driven CRUD helpers the thin ``app/routers/pbc.py`` delegates its writes
    to (the router owns no ``db.commit``; api-contract AP-LAYER-02).

v1 = human-asserted links only; there is NO LLM candidate-suggestion lane (R-Dim D2 §G3).
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date

from sqlalchemy.orm import Session

from ..models import (
    AuditEntry,
    Document,
    InternalActionExecution,
    PBCEvidenceLink,
    PBCItem,
    PBCRequest,
    PBCTemplate,
    ProposedAction,
    QBOActionExecution,
)
from .tenant_companies import organization_company_for_realm, organization_for_company

logger = logging.getLogger(__name__)


class PBCError(Exception):
    """A human-driven PBC write could not proceed (unmapped realm, bad category).
    Raised by the service; the router maps it to the one error envelope."""


# ── v1 PBC category set (audit-support / month-end-close domain) ──────────────
PBC_CATEGORIES = (
    "ENGAGEMENT_LETTER",
    "BANK_STATEMENTS",
    "BANK_RECONCILIATIONS",
    "AR_AGING",
    "AP_AGING",
    "FIXED_ASSET_ROLLFORWARD",
    "DEBT_AGREEMENTS",
    "JOURNAL_ENTRY_SUPPORT",
    "TAX_RETURNS",
    "OTHER",
)

ENGAGEMENT_TYPES = ("AUDIT", "TAX", "REVIEW")
REQUEST_STATUSES = ("OPEN", "IN_PROGRESS", "DELIVERED")
ITEM_STATUSES = ("OPEN", "PROVIDED", "ACCEPTED", "NA")

# The assemble-never-judge boundary, carried on every assembled binder payload.
BINDER_NOTICE = (
    "PBC evidence binder — an ASSEMBLY of human-asserted links to real, same-tenant "
    "evidence. Each link is re-resolved under the tenant scope at render: LINKED when a "
    "real owned row resolves, NO_DATA otherwise. The binder NEVER fabricates or infers a "
    "link and NEVER marks a line satisfied — a line is provided only by a human action. "
    "Requires human review."
)

# One seed PBCTemplate (materialized per-tenant by ``ensure_default_template``).
DEFAULT_PBC_TEMPLATE: dict[str, object] = {
    "name": "Standard PBC Request List",
    "engagement_type": "AUDIT",
    "description": "Default prepared-by-client checklist for a year-end audit/review.",
    "items": [
        {"category": "ENGAGEMENT_LETTER", "title": "Signed engagement letter", "priority": "high", "sort_order": 10},
        {"category": "BANK_STATEMENTS", "title": "Year-end bank statements (all accounts)", "priority": "high", "sort_order": 20},
        {"category": "BANK_RECONCILIATIONS", "title": "Year-end bank reconciliations", "priority": "high", "sort_order": 30},
        {"category": "AR_AGING", "title": "AR aging at period end", "priority": "normal", "sort_order": 40},
        {"category": "AP_AGING", "title": "AP aging at period end", "priority": "normal", "sort_order": 50},
        {"category": "FIXED_ASSET_ROLLFORWARD", "title": "Fixed-asset roll-forward + depreciation schedule", "priority": "normal", "sort_order": 60},
        {"category": "DEBT_AGREEMENTS", "title": "Debt agreements + covenant compliance", "priority": "normal", "sort_order": 70},
        {"category": "JOURNAL_ENTRY_SUPPORT", "title": "Support for material journal entries", "priority": "normal", "sort_order": 80},
        {"category": "TAX_RETURNS", "title": "Prior-year tax returns", "priority": "low", "sort_order": 90},
    ],
}


# ── The evidence resolvers — every query filters company_id == realm_id ───────
# A forged / foreign-tenant / non-existent ref returns nothing → NO_DATA. Each is a
# BATCH resolver (one `.in_(ids)` query per evidence type) so the assembler never
# issues a per-row query (no N+1).

def _batch_proposal(db: Session, realm_id: str, ids: list[int]) -> dict[int, dict[str, object]]:
    rows = (
        db.query(ProposedAction)
        .filter(ProposedAction.company_id == realm_id, ProposedAction.id.in_(ids))
        .all()
    )
    return {r.id: {"proposed_action_id": r.id, "action_type": r.action_type, "status": r.status} for r in rows}


def _batch_qbo_execution(db: Session, realm_id: str, ids: list[int]) -> dict[int, dict[str, object]]:
    rows = (
        db.query(QBOActionExecution)
        .filter(QBOActionExecution.company_id == realm_id, QBOActionExecution.id.in_(ids))
        .all()
    )
    return {
        r.id: {"execution_id": r.id, "action_type": r.action_type, "status": r.status, "provider_request_id": r.provider_request_id}
        for r in rows
    }


def _batch_internal_execution(db: Session, realm_id: str, ids: list[int]) -> dict[int, dict[str, object]]:
    rows = (
        db.query(InternalActionExecution)
        .filter(InternalActionExecution.company_id == realm_id, InternalActionExecution.id.in_(ids))
        .all()
    )
    return {r.id: {"execution_id": r.id, "tool_name": r.tool_name, "status": r.status} for r in rows}


def _batch_document(db: Session, realm_id: str, ids: list[int]) -> dict[int, dict[str, object]]:
    rows = (
        db.query(Document)
        .filter(Document.company_id == realm_id, Document.id.in_(ids))
        .all()
    )
    return {r.id: {"document_id": r.id, "filename": r.filename, "document_type": r.document_type} for r in rows}


def _batch_audit_entry(db: Session, realm_id: str, ids: list[int]) -> dict[int, dict[str, object]]:
    # Read AuditEntry DIRECTLY — the merged-audit helper (fetch_merged_audit_logs)
    # unions only SystemAuditLog + AuditLog, NOT AuditEntry (R-Dim D2 §1.5).
    rows = (
        db.query(AuditEntry)
        .filter(AuditEntry.company_id == realm_id, AuditEntry.id.in_(ids))
        .all()
    )
    return {
        r.id: {"audit_entry_id": r.id, "action": r.action, "entity_type": r.entity_type, "entity_id": r.entity_id}
        for r in rows
    }


EVIDENCE_RESOLVERS: dict[str, Callable[[Session, str, list[int]], dict[int, dict[str, object]]]] = {
    "proposal": _batch_proposal,
    "qbo_execution": _batch_qbo_execution,
    "internal_execution": _batch_internal_execution,
    "document": _batch_document,
    "audit_entry": _batch_audit_entry,
}


def _resolve_links(db: Session, realm_id: str, links: list[PBCEvidenceLink]) -> list[dict[str, object]]:
    """Resolve every link's ref under the tenant scope — batched by evidence type
    so there is one query per type, never one per link. A ref that does not parse to
    an int, or does not resolve to a same-tenant row, is NO_DATA."""
    parsed: list[tuple[PBCEvidenceLink, int | None]] = []
    ids_by_type: dict[str, set[int]] = {}
    for link in links:
        try:
            ref_id: int | None = int(link.evidence_ref)
        except (TypeError, ValueError):
            ref_id = None
        parsed.append((link, ref_id))
        if ref_id is not None and link.evidence_type in EVIDENCE_RESOLVERS:
            ids_by_type.setdefault(link.evidence_type, set()).add(ref_id)

    detail_by_type: dict[str, dict[int, dict[str, object]]] = {}
    for etype, ids in ids_by_type.items():  # SA-OK: bounded-loop 5 — fixed EVIDENCE_RESOLVERS vocab; one batch .in_() query per evidence type, not per link
        detail_by_type[etype] = EVIDENCE_RESOLVERS[etype](db, realm_id, list(ids))

    out: list[dict[str, object]] = []
    for link, ref_id in parsed:
        detail = None
        if ref_id is not None:
            detail = detail_by_type.get(link.evidence_type, {}).get(ref_id)
        out.append(
            {
                "evidence_link_id": link.id,
                "evidence_type": link.evidence_type,
                "evidence_ref": link.evidence_ref,
                "label": link.label,
                # Re-resolved fresh — the stored link.link_state is NOT trusted here.
                "resolution": "LINKED" if detail is not None else "NO_DATA",
                "detail": detail,
            }
        )
    return out


def assemble_binder(db: Session, request: PBCRequest) -> dict[str, object]:
    """Assemble ONE ``PBCRequest``'s binder (pure read). For each line, resolve its
    human-asserted evidence links under the tenant scope; the line ``link_state`` is
    LINKED iff ≥1 link resolves, else NO_DATA. The line ``status`` is returned
    UNCHANGED — the binder never auto-satisfies a line."""
    realm_id = request.company_id
    items = (
        db.query(PBCItem)
        .filter(PBCItem.company_id == realm_id, PBCItem.pbc_request_id == request.id)
        .order_by(PBCItem.sort_order, PBCItem.id)
        .all()
    )
    item_ids = [it.id for it in items]
    links = (
        db.query(PBCEvidenceLink)
        .filter(PBCEvidenceLink.company_id == realm_id, PBCEvidenceLink.pbc_item_id.in_(item_ids))
        .order_by(PBCEvidenceLink.id)
        .all()
        if item_ids
        else []
    )
    links_by_item: dict[int, list[PBCEvidenceLink]] = {}
    for link in links:
        links_by_item.setdefault(link.pbc_item_id, []).append(link)

    item_out: list[dict[str, object]] = []
    for item in items:
        resolved = _resolve_links(db, realm_id, links_by_item.get(item.id, []))
        link_state = "LINKED" if any(r["resolution"] == "LINKED" for r in resolved) else "NO_DATA"
        item_out.append(
            {
                "item_id": item.id,
                "category": item.category,
                "title": item.title,
                "status": item.status,  # UNCHANGED — never auto-satisfied
                "link_state": link_state,
                "evidence": resolved,
            }
        )
    return {
        "request_id": request.id,
        "engagement_name": request.engagement_name,
        "engagement_type": request.engagement_type,
        "status": request.status,
        "requires_human_review": True,
        "notice": BINDER_NOTICE,
        "items": item_out,
    }


def run_pbc_binder(db: Session, realm_id: str, *, pbc_request_id: int | None = None) -> dict[str, object]:
    """Assemble the binders for a realm (one specific request, or every not-yet-delivered
    request). The pure-reader runner deliverable — persisted as an InsightArtifact."""
    query = db.query(PBCRequest).filter(PBCRequest.company_id == realm_id)
    if pbc_request_id is not None:
        query = query.filter(PBCRequest.id == pbc_request_id)
    else:
        query = query.filter(PBCRequest.status != "DELIVERED")
    requests = query.order_by(PBCRequest.id).all()

    binders = [assemble_binder(db, r) for r in requests]
    total_lines = sum(len(b["items"]) for b in binders)  # type: ignore[arg-type]  # CRAFT-OK: b["items"] is the assembled list[dict] built above
    lines_linked = sum(
        1 for b in binders for it in b["items"] if it["link_state"] == "LINKED"  # type: ignore[index,union-attr]  # CRAFT-OK: iterating the assembled binder dicts built above
    )
    return {
        "package_type": "pbc_binder",
        "requires_human_review": True,
        "notice": BINDER_NOTICE,
        "binders": binders,
        "totals": {"requests": len(binders), "lines": total_lines, "lines_linked": lines_linked},
        "source_refs": [{"type": "pbc_binder"}, {"type": "pbc_evidence_links"}],
        "provenance": {
            "assembler": "app/services/pbc_binder.run_pbc_binder",
            "boundary": (
                "assemble-never-judge — links only real, same-tenant, human-asserted "
                "evidence; never fabricates a link or auto-satisfies a line"
            ),
        },
    }


# ── Human-driven CRUD (the router delegates its writes here) ──────────────────

def _resolve_scope(db: Session, realm_id: str) -> tuple[int, int | None]:
    """Resolve the (organization_id, organization_company_id) for a realm. The caller
    has already passed the tenant membership gate; a genuinely unmapped realm is a
    server-side inconsistency → PBCError."""
    org = organization_for_company(db, realm_id)
    if org is None:
        raise PBCError(f"realm {realm_id} maps to no organization")
    org_company = organization_company_for_realm(db, organization_id=org.id, realm_id=realm_id)
    return org.id, (org_company.id if org_company else None)


def _validate_category(category: str) -> str:
    if category not in PBC_CATEGORIES:
        raise PBCError(f"unknown PBC category '{category}' (expected one of {', '.join(PBC_CATEGORIES)})")
    return category


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise PBCError(f"invalid date '{value}' (expected ISO YYYY-MM-DD)") from exc


def create_pbc_request(
    db: Session,
    *,
    realm_id: str,
    engagement_name: str,
    engagement_type: str = "AUDIT",
    period_start: str | None = None,
    period_end: str | None = None,
    created_by: str | None = None,
    items: list[dict[str, object]] | None = None,
) -> PBCRequest:
    if engagement_type not in ENGAGEMENT_TYPES:
        raise PBCError(f"unknown engagement_type '{engagement_type}'")
    org_id, org_company_id = _resolve_scope(db, realm_id)
    request = PBCRequest(
        organization_id=org_id,
        organization_company_id=org_company_id,
        company_id=realm_id,
        engagement_name=engagement_name,
        engagement_type=engagement_type,
        period_start=_parse_date(period_start),
        period_end=_parse_date(period_end),
        status="OPEN",
        created_by=created_by,
    )
    db.add(request)
    db.flush()  # assign request.id for the child items below
    for defn in items or []:
        _add_item_row(db, request, defn)
    db.commit()
    db.refresh(request)
    return request


def _add_item_row(db: Session, request: PBCRequest, defn: dict[str, object]) -> PBCItem:
    item = PBCItem(
        organization_id=request.organization_id,
        organization_company_id=request.organization_company_id,
        company_id=request.company_id,
        pbc_request_id=request.id,
        category=_validate_category(str(defn.get("category", "OTHER"))),
        title=str(defn["title"]),
        description=defn.get("description"),
        assigned_to=defn.get("assigned_to"),
        requested_by=defn.get("requested_by"),
        due_date=_parse_date(defn.get("due_date")),  # type: ignore[arg-type]  # CRAFT-OK: defn values are str|None from the request DTO
        priority=defn.get("priority", "normal"),
        sort_order=int(defn.get("sort_order", 0)),  # type: ignore[call-overload]  # CRAFT-OK: sort_order is an int from the request DTO
        status="OPEN",
    )
    db.add(item)
    return item


def add_pbc_item(db: Session, request: PBCRequest, defn: dict[str, object]) -> PBCItem:
    item = _add_item_row(db, request, defn)
    db.commit()
    db.refresh(item)
    return item


def update_pbc_request(db: Session, request: PBCRequest, *, status: str | None = None, engagement_name: str | None = None) -> PBCRequest:
    if status is not None:
        if status not in REQUEST_STATUSES:
            raise PBCError(f"unknown request status '{status}'")
        request.status = status
    if engagement_name is not None:
        request.engagement_name = engagement_name
    db.commit()
    db.refresh(request)
    return request


def update_pbc_item(
    db: Session,
    item: PBCItem,
    *,
    status: str | None = None,
    assigned_to: str | None = None,
    requested_by: str | None = None,
    priority: str | None = None,
    due_date: str | None = None,
) -> PBCItem:
    if status is not None:
        if status not in ITEM_STATUSES:
            raise PBCError(f"unknown item status '{status}'")
        item.status = status
    if assigned_to is not None:
        item.assigned_to = assigned_to
    if requested_by is not None:
        item.requested_by = requested_by
    if priority is not None:
        item.priority = priority
    if due_date is not None:
        item.due_date = _parse_date(due_date)
    db.commit()
    db.refresh(item)
    return item


def add_evidence_link(
    db: Session,
    item: PBCItem,
    *,
    evidence_type: str,
    evidence_ref: str,
    label: str | None = None,
    linked_by: str | None = None,
) -> PBCEvidenceLink:
    if evidence_type not in EVIDENCE_RESOLVERS:
        raise PBCError(f"unknown evidence_type '{evidence_type}' (expected one of {', '.join(EVIDENCE_RESOLVERS)})")
    link = PBCEvidenceLink(
        organization_id=item.organization_id,
        organization_company_id=item.organization_company_id,
        company_id=item.company_id,
        pbc_item_id=item.id,
        evidence_type=evidence_type,
        evidence_ref=str(evidence_ref),
        label=label,
        link_state="ASSERTED",
        linked_by=linked_by,
    )
    db.add(link)
    db.commit()
    db.refresh(link)
    return link


def list_pbc_requests(db: Session, realm_id: str) -> list[PBCRequest]:
    return (
        db.query(PBCRequest)
        .filter(PBCRequest.company_id == realm_id)
        .order_by(PBCRequest.id.desc())
        .all()
    )


def list_request_items(db: Session, realm_id: str, request_id: int) -> list[PBCItem]:
    return (
        db.query(PBCItem)
        .filter(PBCItem.company_id == realm_id, PBCItem.pbc_request_id == request_id)
        .order_by(PBCItem.sort_order, PBCItem.id)
        .all()
    )


def ensure_default_template(db: Session, realm_id: str) -> PBCTemplate:
    """Materialize the ONE seed PBCTemplate for a tenant (idempotent — returns the
    existing default if present)."""
    existing = (
        db.query(PBCTemplate)
        .filter(PBCTemplate.company_id == realm_id, PBCTemplate.is_default.is_(True))
        .first()
    )
    if existing:
        return existing
    org_id, org_company_id = _resolve_scope(db, realm_id)
    template = PBCTemplate(
        organization_id=org_id,
        organization_company_id=org_company_id,
        company_id=realm_id,
        name=DEFAULT_PBC_TEMPLATE["name"],
        engagement_type=DEFAULT_PBC_TEMPLATE["engagement_type"],
        description=DEFAULT_PBC_TEMPLATE["description"],
        is_default=True,
        items=DEFAULT_PBC_TEMPLATE["items"],
    )
    db.add(template)
    db.commit()
    db.refresh(template)
    return template


def list_pbc_templates(db: Session, realm_id: str) -> list[PBCTemplate]:
    return (
        db.query(PBCTemplate)
        .filter(PBCTemplate.company_id == realm_id)
        .order_by(PBCTemplate.id.desc())
        .all()
    )

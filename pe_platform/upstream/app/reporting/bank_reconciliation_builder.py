# PLAN-EXEMPT: bank-reconciliation payload builder under ratified plan 20260701-bank-reconciliation (AC-1.4); a NEW builder module kept separate from named_report_builders.py (already 452 lines, near the 500 cap) — the same split covenant_monthly_builder.py uses, so neither file trips the file-size deny.
"""
app/reporting/bank_reconciliation_builder.py
──────────────────────────────────────────────
Payload builder for the bank-reconciliation report (plan 20260701-bank-reconciliation,
AC-1.4). Assembles, for the reconciliation session that matches realm=company_id + the
requested period, the tie-out lines a bank statement reconciliation reproduces:

    opening balance · cleared deposits (Σ cleared > 0) · cleared withdrawals (Σ cleared < 0)
    · cleared balance · statement ending balance · difference · the outstanding/uncleared list.

DISPLAY-ONLY (AP-DR-06 / financial-modeling-correctness): the ONLY derivation is the
call to ``session_svc.compute_tieout(...)`` — a display of the tie-out DTO the session
service already computed in Decimal. This builder re-derives NO difference and NO cleared
balance; it splits the cleared txns into deposits/withdrawals for presentation only, and
those two sub-totals reconcile to ``tieout.cleared_sum`` (never re-summed into the tie-out).
So the rendered figures tie exactly to ``compute_tieout`` (and its golden test).

Honest empty-state (AC-1.4 / DEC-BREC): when no session matches the realm+period, the
payload carries ``available=False`` + a reason — never a fabricated all-zero tie-out. An
un-anchored session (``anchor_configured=False``) surfaces that the opening 0 is a
placeholder, not a real baseline.

TENANT-SAFETY PRECONDITION (AP-DR-07 / multi-tenancy-app-layer): the caller
(routers/reporting.py `download_report`) gates tenant access via
``check_tenant_access("VIEWER", company_id=...)`` BEFORE invoking a builder, which pins the
realm GUC; every query here ALSO carries an explicit ``realm_id == company_id`` predicate
(the app-layer PRIMARY gate, which runs on the SQLite test lane that has no RLS). Never
authorize off a client-supplied realm.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any  # CRAFT-OK: Any = opaque QBOClient boundary + dynamic report payload

from sqlalchemy.orm import Session

from ..models import QBOTransaction, ReconciliationSession
from ..qbo_client import get_qbo_client_from_db
from ..services.bank_recon import match as match_svc
from ..services.bank_recon import session as session_svc

# A report payload is dynamic provider-derived JSON of varying shape.
Payload = dict[str, Any]  # CRAFT-OK: dynamic provider-derived reconciliation payload boundary

_ZERO = Decimal("0.00")


def _metadata(company_id: str, period: str, company_name: str, title: str) -> Payload:
    return {
        "company_id": company_id,
        "company_name": company_name,
        "period": period,
        "period_label": period,
        "title": title,
        "generated_at": datetime.utcnow().strftime("%B %d, %Y at %I:%M %p UTC"),
    }


def _company_name(db: Session, company_id: str) -> str:
    """Best-effort company name from QBO CompanyInfo; blank on failure (display-only,
    never blocks the report)."""
    try:
        client = get_qbo_client_from_db(db, company_id)
        if not client:
            return ""
        info = client.get_company_info()
        return info.get("CompanyInfo", {}).get("CompanyName", "") or ""
    except Exception:
        return ""


def _resolve_session(
    db: Session, company_id: str, period: str
) -> ReconciliationSession | None:
    """Resolve the reconciliation session for realm=company_id whose statement period
    covers ``period`` (a ``YYYY-MM``). A session's ``period_end`` month is the statement
    month; we match on it and pick the most recently created when several exist. Returns
    None (→ honest empty-state) when nothing matches — never fabricates one.

    Realm-scoped: filters on ``realm_id == company_id`` (the app-layer PRIMARY gate). The
    caller has membership-gated + pinned the realm GUC via ``check_tenant_access`` first.
    """
    query = db.query(ReconciliationSession).filter(
        ReconciliationSession.realm_id == company_id,
        # An ABANDONED session is a discarded draft (B5-2) — never the report's
        # subject; without this filter an abandoned re-create shadows the real,
        # completed reconciliation for the month (latest-created wins below).
        ReconciliationSession.status != session_svc.STATUS_ABANDONED,
    )
    year_month = str(period or "").strip()
    if len(year_month) == 7 and year_month[4] == "-":
        try:
            year, month = int(year_month[:4]), int(year_month[5:7])
        except ValueError:
            year = month = 0
        if year and month:
            # Match on the statement (period_end) month via a portable year/month
            # extract over the Date column — works on SQLite (test lane) and Postgres.
            from sqlalchemy import extract

            query = query.filter(
                extract("year", ReconciliationSession.period_end) == year,
                extract("month", ReconciliationSession.period_end) == month,
            )
    return query.order_by(ReconciliationSession.created_at.desc()).first()


# The (deposits, withdrawals) split is ``session_svc.cleared_split`` — the ONE canonical,
# SIGNED implementation (B5-3). The private raw-column copy that lived here counted every
# unsigned QBO withdrawal as a deposit (the "deposits 51,533.53 / withdrawals 0.00" report
# symptom) — the tie-out router had been fixed but this third copy had not. Deleted; the
# builder now consumes the same service function the tie-out endpoint uses, so the rendered
# report can never diverge from the tie-out panel on the sign convention.


def _outstanding_rows(
    items: list[QBOTransaction],
) -> list[Payload]:
    """Shape the outstanding (uncleared) book txns into display rows — date/description
    carried VERBATIM from the ORM rows the session service returned; the amount is the
    DIRECTION-AWARE signed figure (``match.signed_amount``, B5-4 — the stored QBO amount
    is unsigned, so a raw carry rendered withdrawals positive). The free-text
    (description/type) is escaped/sanitized at the RENDER edge (_xml / _safe_text), never
    here."""
    out: list[Payload] = []
    for txn in items:  # SA-OK: bounded-loop over the pre-fetched outstanding list, not per-row DB access
        out.append({
            "date": txn.date.isoformat() if txn.date else "",
            "description": txn.description or txn.transaction_type or "",
            "amount": match_svc.signed_amount(txn),  # Decimal; formatted at the render edge
        })
    return out


def build_bank_reconciliation(db: Session, company_id: str, period: str) -> Payload:
    """AC-1.4 — assemble the bank-reconciliation report payload for the session matching
    realm=company_id + ``period``. DISPLAY-ONLY: the tie-out figures come straight from
    ``session_svc.compute_tieout`` and the outstanding list from
    ``session_svc.outstanding_items`` — the builder re-derives NO difference. When no
    session matches, an honest empty-state is returned (no fabricated $0)."""
    company_name = _company_name(db, company_id)
    session = _resolve_session(db, company_id, period)

    if session is None:
        return {
            "metadata": _metadata(
                company_id, period, company_name,
                f"Bank Reconciliation — {period}",
            ),
            "reconciliation": {
                "available": False,
                "reason": (
                    "No bank reconciliation session was found for this account and "
                    "period. Start a reconciliation to generate this report."
                ),
            },
            "errors": [],
            "data_quality": {"is_partial": False, "unavailable_sections": []},
        }

    tieout = session_svc.compute_tieout(db, realm_id=company_id, session=session)
    deposits, withdrawals = session_svc.cleared_split(
        db, realm_id=company_id, session_id=session.id
    )
    outstanding = session_svc.outstanding_items(
        db, realm_id=company_id, session=session)

    reconciliation: Payload = {
        "available": True,
        "account_name": session.account_name,
        "account_id": session.account_id,
        "period_start": session.period_start.isoformat() if session.period_start else "",
        "period_end": session.period_end.isoformat() if session.period_end else "",
        "status": session.status,
        # Tie-out figures — carried VERBATIM from the compute_tieout DTO (display-only).
        "opening_balance": tieout.opening,
        "anchor_configured": tieout.anchor_configured,
        "cleared_deposits": deposits,
        "cleared_withdrawals": withdrawals,
        "cleared_balance": tieout.cleared_balance,
        "statement_ending_balance": tieout.ending_balance,
        "difference": tieout.difference,
        "is_balanced": tieout.difference == _ZERO,
        "outstanding_items": _outstanding_rows(outstanding),
    }

    return {
        "metadata": _metadata(
            company_id, period, company_name,
            f"Bank Reconciliation — {period}",
        ),
        "reconciliation": reconciliation,
        "errors": [],
        "data_quality": {"is_partial": False, "unavailable_sections": []},
    }

"""Fixed-asset / intangible disposal — the callable service.

Extracted from ``app/routers/accounting_schedules.py::dispose_schedule`` so the
disposal accounting (NBV, gain/loss, the partial-period convention) and the
proposal-drafting are reachable OUTSIDE an HTTP request — e.g. by a
proposal-gated agent tool. The router endpoint is a thin caller over this.

Lane: the disposal drafts an INTERNAL-DB ``ProposedAction`` (action_type
``JOURNAL_ENTRY``, status ``PENDING``) via ``je_builder.build_disposal_je`` —
it is NEVER auto-posted and NOTHING posts to QuickBooks here. A human approves
it in the proposals workflow; the schedule-engine post-execution hook flips the
schedule to DISPOSED on execution.

Math (Decimal-safe, bit-identical to the legacy router path):
    NBV       = cost − accumulated depreciation at the disposal date
    gain/loss = proceeds − NBV
PARTIAL-PERIOD CONVENTION: NO depreciation is taken in the disposal month —
accumulated is measured from what is actually POSTED, and unposted entries
at/after the disposal month block the disposal until resolved or skipped.

Errors: this service raises ``ScheduleDisposalError(status_code, message)`` for
the disposal-specific 400/409 conditions (the router maps each 1:1 to an
``HTTPException`` with the SAME status + message). It does NOT catch the
``assert_period_writable`` locked-period guard, which raises its own
``user_safe_http_exception`` — that HTTPException propagates unchanged, exactly
as it did from the router body.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session

if TYPE_CHECKING:  # pragma: no cover — typing only
    from ..models import AccountingSchedule, ProposedAction


@dataclass
class ScheduleDisposalError(Exception):
    """A disposal-specific rejection carrying the EXACT HTTP status + message
    the legacy router raised. The router maps it 1:1 to an ``HTTPException`` so
    the request/response/status contract stays byte-identical."""

    status_code: int
    message: str

    def __str__(self) -> str:  # pragma: no cover — trivial
        return self.message


@dataclass
class DisposalResult:
    """Plain-data return: the drafted proposal plus the disposal metadata the
    router echoes to the client. ``draft_entries_skipped`` counts DRAFT rows the
    service marked SKIPPED (a side effect flushed under the caller's session)."""

    proposal: "ProposedAction"
    disposal_meta: dict
    draft_entries_skipped: int


def dispose_schedule_service(
    db: Session,
    schedule: "AccountingSchedule",
    *,
    disposal_date: date,
    proceeds: float = 0,
    proceeds_account_id: str | None = None,
    proceeds_account_name: str | None = None,
    asset_cost_account_id: str | None = None,
    asset_cost_account_name: str | None = None,
    gain_loss_account_id: str | None = None,
    gain_loss_account_name: str | None = None,
    skip_draft_entries: bool = False,
    submitted_by: str,
    human_preparer_user_id: int | None = None,
) -> DisposalResult:
    """Compute the disposal + draft the PROPOSAL-GATED disposal JE for ``schedule``.

    The caller is responsible for the tenant/ownership gate (the router uses
    ``check_entity_tenant_access`` before calling this). This service owns the
    write transaction: it drafts the proposal and ``commit``s, so a non-HTTP
    caller (e.g. a proposal-gated agent tool) gets a committed
    ``ProposedAction`` back with no further work.

    Returns a :class:`DisposalResult`. Raises :class:`ScheduleDisposalError` for
    the disposal-specific 400/409 conditions; lets the locked-period guard's own
    HTTPException propagate.
    """
    from ..models import ProposedAction, ScheduleCreditMapping, ScheduleEntry
    from .accounting import je_builder
    from .schedule_engine import _resolve_account_id
    from .schedule_types import SCHEDULE_TYPES
    from .task_autonomy import assert_period_writable

    s = schedule

    descriptor = SCHEDULE_TYPES.get(s.schedule_type or "")
    if not descriptor or not descriptor.supports_disposal:
        raise ScheduleDisposalError(
            400,
            f"Disposal applies to fixed-asset/intangible schedules; this schedule is "
            f"{s.schedule_type}. Delete or complete it instead.",
        )
    if s.status == "DISPOSED":
        raise ScheduleDisposalError(409, "This schedule is already disposed.")
    if s.status not in ("ACTIVE", "FULLY_AMORTIZED"):
        raise ScheduleDisposalError(
            409, f"Schedule is {s.status}; approve it before disposing."
        )
    if proceeds < 0:
        raise ScheduleDisposalError(400, "proceeds must be >= 0")

    # Double-dispose guard: a live (PENDING/APPROVED) disposal proposal blocks
    # a second draft.
    if s.disposal_proposed_action_id:
        prior = (
            db.query(ProposedAction)
            .filter(
                ProposedAction.id == s.disposal_proposed_action_id,
                ProposedAction.company_id == s.realm_id,
            )
            .first()
        )
        if prior and prior.status in ("PENDING", "APPROVED"):
            raise ScheduleDisposalError(
                409,
                f"A disposal proposal (#{prior.id}, {prior.status}) is already awaiting "
                f"review for this schedule. Approve or reject it first.",
            )

    # Persist an explicitly-provided cost account, then require one. The cost
    # account is a SEPARATE column from qbo_account_id because that column's
    # FA meaning is ambiguous on real data (see models.py) — a disposal JE
    # that guessed would credit the wrong side of the asset.
    if asset_cost_account_id:
        s.asset_cost_account_id = str(asset_cost_account_id)
        s.asset_cost_account_name = asset_cost_account_name or s.asset_cost_account_name
    if not s.asset_cost_account_id:
        raise ScheduleDisposalError(
            409,
            "This schedule has no asset cost account set. Set asset_cost_account_id "
            "(the QBO fixed-asset COST account the purchase was capitalized to) on "
            "the schedule or pass it in the dispose request — the disposal JE must "
            "credit the cost account to remove the asset.",
        )

    disposal_month = disposal_date.strftime("%Y-%m")
    # Locked-period guard (B5-8 cascade included): fail-closed 409. Raises its
    # own user_safe_http_exception — propagates unchanged, exactly as before.
    assert_period_writable(db, realm_id=s.realm_id, period=disposal_month)

    # Unposted entries at/after the disposal month must be resolved first —
    # they are amounts the books haven't decided on. PENDING_APPROVAL rows
    # (a live JE proposal) ALWAYS block; plain DRAFT rows block unless the
    # caller asked us to skip them (skip_draft_entries).
    entries = (
        db.query(ScheduleEntry)
        .filter(
            ScheduleEntry.schedule_id == s.id,
            ScheduleEntry.realm_id == s.realm_id,
        )
        .all()
    )
    unposted_at_or_after = [
        e
        for e in entries
        if e.period[:7] >= disposal_month and e.status in ("DRAFT", "PENDING_APPROVAL")
    ]
    pending = [e for e in unposted_at_or_after if e.status == "PENDING_APPROVAL"]
    drafts = [e for e in unposted_at_or_after if e.status == "DRAFT"]
    if pending:
        raise ScheduleDisposalError(
            409,
            f"{len(pending)} schedule entr{'y' if len(pending) == 1 else 'ies'} at/after "
            f"{disposal_month} {'is' if len(pending) == 1 else 'are'} awaiting approval "
            f"(periods: {', '.join(sorted(e.period for e in pending)[:6])}). Approve or "
            f"reject those proposals before disposing — no depreciation is taken in the "
            f"disposal month.",
        )
    if drafts and not skip_draft_entries:
        raise ScheduleDisposalError(
            409,
            f"{len(drafts)} draft depreciation entr{'y' if len(drafts) == 1 else 'ies'} "
            f"remain at/after {disposal_month}. Re-send with skip_draft_entries=true to "
            f"skip them (no depreciation is taken in the disposal month), or resolve "
            f"them first.",
        )
    for e in drafts:
        e.status = "SKIPPED"

    # Accumulated depreciation at disposal = pre-Peregrine accumulated + every
    # POSTED entry (the books' truth — the JE removes what is actually on the
    # contra account for this asset).
    accumulated = Decimal(str(s.accumulated_amount or 0)) + sum(
        (Decimal(str(e.amount or 0)) for e in entries if e.status == "POSTED"),
        Decimal("0"),
    )
    cost = Decimal(str(s.original_amount or 0))
    if cost <= 0:
        raise ScheduleDisposalError(
            400, "Only a schedule with a positive cost can be disposed."
        )

    proceeds_dec = Decimal(str(proceeds)).quantize(Decimal("0.01"))
    if proceeds_dec > 0 and not proceeds_account_id:
        raise ScheduleDisposalError(
            409,
            "proceeds_account_id is required when proceeds > 0 — the account the "
            "sale proceeds were (or will be) deposited to (bank, undeposited funds, "
            "or a receivable).",
        )

    # Contra (accumulated depreciation) account: resolved exactly like monthly
    # generation — account-level mapping (keyed by the schedule's BS account)
    # → type-level default → the schedule's own BS account as last resort.
    contra_id, contra_name = None, ""
    mapping_rows = (
        db.query(ScheduleCreditMapping)
        .filter(
            ScheduleCreditMapping.realm_id == s.realm_id,
            ScheduleCreditMapping.schedule_type == s.schedule_type,
        )
        .order_by(ScheduleCreditMapping.id)
        .all()
    )
    account_level = next(
        (
            m
            for m in mapping_rows
            if m.source_account_id and s.qbo_account_id
            and str(m.source_account_id) == str(s.qbo_account_id)
        ),
        None,
    )
    type_level = next((m for m in mapping_rows if not m.source_account_id), None)
    resolved = account_level or type_level
    if resolved:
        contra_id, contra_name = str(resolved.qbo_account_id), resolved.qbo_account_name or ""
    elif s.qbo_account_id:
        contra_id, contra_name = str(s.qbo_account_id), s.qbo_account_name or ""
    if not contra_id:
        raise ScheduleDisposalError(
            409,
            f"No accumulated-depreciation account is mapped for {s.schedule_type} "
            f"schedules. Set the BS credit account mapping (the contra account monthly "
            f"depreciation credits into) before disposing.",
        )
    if str(contra_id) == str(s.asset_cost_account_id):
        raise ScheduleDisposalError(
            409,
            "The resolved accumulated-depreciation account equals the asset cost "
            "account. Map the contra (accumulated depreciation) account for this "
            "schedule type — the disposal JE must debit the contra and credit the "
            "cost account.",
        )

    # Gain/loss P&L account: explicit override, else the registry default name
    # resolved against the local QBO account cache.
    gl_id = gain_loss_account_id
    gl_name = gain_loss_account_name or descriptor.default_gain_loss_account_name
    if not gl_id:
        gl_id = _resolve_account_id(db, s.realm_id, gl_name)
    if not gl_id:
        raise ScheduleDisposalError(
            409,
            f"Could not resolve a '{gl_name}' account in this company's chart of "
            f"accounts. Create it in QuickBooks (an Other Income/Expense account) or "
            f"pass gain_loss_account_id explicitly.",
        )

    proposal = je_builder.build_disposal_je(
        s,
        db,
        disposal_date=disposal_date,
        cost=cost,
        accumulated=accumulated,
        proceeds=proceeds_dec,
        cost_account=(str(s.asset_cost_account_id), s.asset_cost_account_name or ""),
        contra_account=(str(contra_id), contra_name),
        proceeds_account=(
            (str(proceeds_account_id), proceeds_account_name or "")
            if proceeds_dec > 0
            else None
        ),
        gain_loss_account=(str(gl_id), gl_name),
        submitted_by=submitted_by,
        human_preparer_user_id=human_preparer_user_id,
    )
    s.disposal_proposed_action_id = proposal.id
    db.commit()

    return DisposalResult(
        proposal=proposal,
        disposal_meta=proposal.payload["peregrine_disposal"],
        draft_entries_skipped=len(drafts),
    )

"""Deterministic Invariant Rails for Financial Actions & Proposals.

Checks supplied payload/context when explicitly called. This standalone utility
is not installed ahead of every live tool or proposal; application authorization
and posting controls are implemented separately. Its checks include:
1. Double-entry balance: Sum(Debits) == Sum(Credits) within $0.01.
2. Non-negative entry amounts (no negative debits/credits).
3. Active accounting period: Prohibits postings into closed periods without explicit reopen;
   with a closed-period boundary set, an entry date that cannot be read fails the check.
4. Segregation of Duties (SoD): Preparer != Approver, shown only from a server-side
   preparer id; only a principal the caller declares human (``approver_principal_type``)
   can approve. AI vs human is never inferred from an id string, and nothing in the
   proposal payload sets it.
5. Direct-write gate: Ensures all mutations stage through ProposedAction ledger.
6. Amount integrity: every leg carries exactly one positive, finite, parseable side.

Action types without rails (anything but journal-shaped actions) return
``passed=False`` with ``UNSUPPORTED_ACTION_TYPE``: an unevaluated action never passes.
"""
from __future__ import annotations

import dataclasses
import datetime
import logging
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class InvariantSeverity(str, Enum):
    CRITICAL = "CRITICAL"
    WARNING = "WARNING"


class InvariantViolationCode(str, Enum):
    UNBALANCED_JOURNAL_ENTRY = "UNBALANCED_JOURNAL_ENTRY"
    ZERO_AMOUNT_TRANSACTION = "ZERO_AMOUNT_TRANSACTION"
    NEGATIVE_ENTRY_AMOUNT = "NEGATIVE_ENTRY_AMOUNT"
    MISSING_ACCOUNT_IDENTIFIER = "MISSING_ACCOUNT_IDENTIFIER"
    CLOSED_ACCOUNTING_PERIOD = "CLOSED_ACCOUNTING_PERIOD"
    PERIOD_UNVERIFIABLE = "PERIOD_UNVERIFIABLE"
    SOD_VIOLATION = "SOD_VIOLATION"
    AI_SELF_APPROVAL = "AI_SELF_APPROVAL"
    DIRECT_WRITE_ATTEMPT = "DIRECT_WRITE_ATTEMPT"
    INVALID_PAYLOAD_STRUCTURE = "INVALID_PAYLOAD_STRUCTURE"
    INVALID_AMOUNT = "INVALID_AMOUNT"
    TWO_SIDED_LEG = "TWO_SIDED_LEG"
    ZERO_AMOUNT_LINE = "ZERO_AMOUNT_LINE"
    APPROVER_PRINCIPAL_UNVERIFIED = "APPROVER_PRINCIPAL_UNVERIFIED"
    PREPARER_PRINCIPAL_UNVERIFIED = "PREPARER_PRINCIPAL_UNVERIFIED"
    UNSUPPORTED_ACTION_TYPE = "UNSUPPORTED_ACTION_TYPE"


class PrincipalType(str, Enum):
    """Who a preparer/approver is, as established by the caller's server-side identity."""

    HUMAN = "human"
    AI_AGENT = "ai_agent"
    SYSTEM = "system"


def _principal_type(value: Any) -> Optional[PrincipalType]:
    """Closed-vocabulary parse; anything else is unknown, never a guess."""
    if value is None or isinstance(value, PrincipalType):
        return value
    try:
        return PrincipalType(str(value).strip().lower())
    except ValueError:
        return None


@dataclass
class InvariantViolation:
    code: InvariantViolationCode
    message: str
    field: Optional[str] = None
    severity: InvariantSeverity = InvariantSeverity.CRITICAL
    details: Dict[str, Any] = dataclasses.field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code.value,
            "message": self.message,
            "field": self.field,
            "severity": self.severity.value,
            "details": self.details,
        }


@dataclass
class RailValidationResult:
    passed: bool
    violations: List[InvariantViolation] = dataclasses.field(default_factory=list)
    metadata: Dict[str, Any] = dataclasses.field(default_factory=dict)

    @property
    def has_critical_violations(self) -> bool:
        return any(v.severity == InvariantSeverity.CRITICAL for v in self.violations)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "passed": self.passed,
            "violations": [v.to_dict() for v in self.violations],
            "metadata": self.metadata,
        }


# Decimal arithmetic outside this exponent range overflows the default context.
_MAX_AMOUNT_EXPONENT = 1000

_LINE_KEYS = ("entries", "lines", "journal_entries", "legs")


def _parse_amount(val: Any) -> Optional[Decimal]:
    """An absent side is 0.00; anything present must be a finite number.

    Returns None for an unparseable, boolean, container, non-finite or out-of-range
    value so the caller rejects it instead of silently counting it as zero.
    """
    if val is None or val == "":
        return Decimal("0.00")
    if isinstance(val, (bool, dict, list, tuple, set)):
        return None
    try:
        parsed = Decimal(str(val).strip())
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not parsed.is_finite() or abs(parsed.adjusted()) > _MAX_AMOUNT_EXPONENT:
        return None
    return parsed


_DATE_KEYS = ("date", "transaction_date", "entry_date")


def _parse_date(val: Any) -> Optional[datetime.date]:
    if not val:
        return None
    # datetime subclasses date, so it is narrowed first: a datetime never compares
    # with a date.
    if isinstance(val, datetime.datetime):
        return val.date()
    if isinstance(val, datetime.date):
        return val
    try:
        return datetime.datetime.fromisoformat(str(val).strip()).date()
    except (TypeError, ValueError):
        return None


class FinancialInvariantRails:
    """Evaluates deterministic financial invariants on AI-generated proposals."""

    def __init__(
        self,
        *,
        materiality_tolerance: Decimal = Decimal("0.01"),
    ):
        self.materiality_tolerance = materiality_tolerance

    def validate_journal_entry_proposal(
        self,
        payload: Dict[str, Any],
        context: Optional[Dict[str, Any]] = None,
    ) -> RailValidationResult:
        """Validate a journal entry proposal against strict financial invariants.

        ``context`` is the caller's server-side view: ``preparer_id``/``approver_id``
        and ``preparer_principal_type``/``approver_principal_type`` (one of
        ``human``, ``ai_agent``, ``system``), plus the closed-period boundary.
        """
        violations: List[InvariantViolation] = []
        ctx = context or {}

        if not isinstance(payload, dict):
            return RailValidationResult(
                passed=False,
                violations=[
                    InvariantViolation(
                        code=InvariantViolationCode.INVALID_PAYLOAD_STRUCTURE,
                        message="Payload must be a dictionary.",
                    )
                ],
            )

        # Extract lines / legs. Legs under two different keys are ambiguous: checking
        # one list would leave the other unvalidated.
        present_line_keys = [key for key in _LINE_KEYS if payload.get(key)]
        if len(present_line_keys) > 1:
            violations.append(
                InvariantViolation(
                    code=InvariantViolationCode.INVALID_PAYLOAD_STRUCTURE,
                    message=f"Journal entry proposal carries legs under more than one key: {present_line_keys}.",
                    field="lines",
                )
            )
            return RailValidationResult(passed=False, violations=violations)
        lines = payload.get(present_line_keys[0]) if present_line_keys else []

        if not lines or not isinstance(lines, list):
            violations.append(
                InvariantViolation(
                    code=InvariantViolationCode.INVALID_PAYLOAD_STRUCTURE,
                    message="Journal entry proposal contains no line items / legs.",
                    field="lines",
                )
            )
            return RailValidationResult(passed=False, violations=violations)

        total_debits = Decimal("0.00")
        total_credits = Decimal("0.00")
        negative_amounts_found = False
        invalid_amounts_found = False

        for idx, line in enumerate(lines):
            if not isinstance(line, dict):
                violations.append(
                    InvariantViolation(
                        code=InvariantViolationCode.INVALID_PAYLOAD_STRUCTURE,
                        message=f"Line item [{idx}] is not an object.",
                        field=f"lines[{idx}]",
                    )
                )
                continue

            # Account identifier check
            acct_num = line.get("account_number") or line.get("account_id")
            acct_name = line.get("account_name") or line.get("account")
            if not acct_num and not acct_name:
                violations.append(
                    InvariantViolation(
                        code=InvariantViolationCode.MISSING_ACCOUNT_IDENTIFIER,
                        message=f"Line [{idx}] lacks both account_number and account_name.",
                        field=f"lines[{idx}]",
                    )
                )

            # Amount validation: an unreadable amount is rejected, never zeroed.
            dr = _parse_amount(line.get("debit"))
            cr = _parse_amount(line.get("credit"))
            if dr is None or cr is None:
                invalid_amounts_found = True
                violations.append(
                    InvariantViolation(
                        code=InvariantViolationCode.INVALID_AMOUNT,
                        message=(
                            f"Line [{idx}] has an amount that is not a finite number "
                            f"(debit={line.get('debit')!r}, credit={line.get('credit')!r})."
                        ),
                        field=f"lines[{idx}]",
                    )
                )
                continue

            if dr < Decimal("0.00") or cr < Decimal("0.00"):
                negative_amounts_found = True
                violations.append(
                    InvariantViolation(
                        code=InvariantViolationCode.NEGATIVE_ENTRY_AMOUNT,
                        message=f"Line [{idx}] has negative amount (dr={dr}, cr={cr}). Opposing entries must be used instead.",
                        field=f"lines[{idx}]",
                    )
                )
            elif dr > Decimal("0.00") and cr > Decimal("0.00"):
                violations.append(
                    InvariantViolation(
                        code=InvariantViolationCode.TWO_SIDED_LEG,
                        message=f"Line [{idx}] carries both a debit ({dr}) and a credit ({cr}); a leg posts one side only.",
                        field=f"lines[{idx}]",
                    )
                )
            elif dr == Decimal("0.00") and cr == Decimal("0.00"):
                violations.append(
                    InvariantViolation(
                        code=InvariantViolationCode.ZERO_AMOUNT_LINE,
                        message=f"Line [{idx}] has neither a debit nor a credit.",
                        field=f"lines[{idx}]",
                    )
                )

            total_debits += dr
            total_credits += cr

        # 1. Check Non-Zero Total
        if (
            total_debits == Decimal("0.00")
            and total_credits == Decimal("0.00")
            and not negative_amounts_found
            and not invalid_amounts_found
        ):
            violations.append(
                InvariantViolation(
                    code=InvariantViolationCode.ZERO_AMOUNT_TRANSACTION,
                    message="Journal entry proposal has zero debits and zero credits.",
                    field="lines",
                )
            )

        # 2. Check Double-Entry Balance (totals exclude rejected amounts, so an
        # entry with any INVALID_AMOUNT is never reported as balanced).
        diff = abs(total_debits - total_credits)
        if diff >= self.materiality_tolerance:
            violations.append(
                InvariantViolation(
                    code=InvariantViolationCode.UNBALANCED_JOURNAL_ENTRY,
                    message=(
                        f"Double-entry out of balance: Debits=${total_debits:.2f} != "
                        f"Credits=${total_credits:.2f} (delta=${diff:.2f})"
                    ),
                    field="lines",
                    details={
                        "total_debits": str(total_debits),
                        "total_credits": str(total_credits),
                        "difference": str(diff),
                    },
                )
            )

        # 3. Check Accounting Period Lock. Once a closed-period boundary is set the
        # lock must be shown to hold: an unreadable boundary, a missing or unreadable
        # entry date, or dates under two keys that disagree fail it, never skip it.
        present_dates = {key: payload.get(key) for key in _DATE_KEYS if payload.get(key)}
        parsed_dates = {key: _parse_date(value) for key, value in present_dates.items()}
        entry_date = next(iter(parsed_dates.values()), None)
        closed_through_raw = ctx.get("closed_period_end_date") or ctx.get("books_closed_through")
        closed_through = _parse_date(closed_through_raw)

        unverifiable_reason: Optional[str] = None
        if closed_through_raw:
            if closed_through is None:
                unverifiable_reason = f"the closed-period boundary {closed_through_raw!r} is not a date"
            elif not present_dates:
                unverifiable_reason = "the entry carries no date"
            elif any(parsed is None for parsed in parsed_dates.values()):
                unverifiable_reason = f"an entry date is not a date ({present_dates!r})"
            elif len(set(parsed_dates.values())) > 1:
                unverifiable_reason = f"the entry carries conflicting dates ({present_dates!r})"
        if unverifiable_reason is not None:
            violations.append(
                InvariantViolation(
                    code=InvariantViolationCode.PERIOD_UNVERIFIABLE,
                    message=(
                        f"The closed-period lock cannot be shown to hold: {unverifiable_reason}."
                    ),
                    field="date",
                    details={"closed_through": str(closed_through_raw)},
                )
            )
        elif entry_date and closed_through and entry_date <= closed_through:
            violations.append(
                InvariantViolation(
                    code=InvariantViolationCode.CLOSED_ACCOUNTING_PERIOD,
                    message=(
                        f"Transaction date {entry_date.isoformat()} falls in a closed accounting period "
                        f"(books closed through {closed_through.isoformat()}). Reopening authorization required."
                    ),
                    field="date",
                    details={
                        "entry_date": entry_date.isoformat(),
                        "closed_through": closed_through.isoformat(),
                    },
                )
            )

        # 4. Check Segregation of Duties (SoD) & Dual-Control. Principal types come
        # only from the caller's context, never from the payload or an id's spelling,
        # and describe only the ids the caller supplied: an approver named solely by
        # the payload ("reviewed_by") stays unverified. A payload-claimed preparer
        # ("created_by") can only add an SoD conflict, never prove its absence.
        server_preparer_id = str(ctx.get("preparer_id") or ctx.get("user_id") or "").strip()
        claimed_preparer_id = str(payload.get("created_by") or "").strip()
        approver_id = str(ctx.get("approver_id") or payload.get("reviewed_by") or "").strip()
        preparer_type = _principal_type(ctx.get("preparer_principal_type"))
        approver_type = (
            _principal_type(ctx.get("approver_principal_type")) if ctx.get("approver_id") else None
        )

        if approver_id:
            # SoD check: preparer cannot self-approve
            self_approving = next(
                (
                    preparer
                    for preparer in (server_preparer_id, claimed_preparer_id)
                    if preparer and preparer.lower() == approver_id.lower()
                ),
                None,
            )
            if self_approving:
                violations.append(
                    InvariantViolation(
                        code=InvariantViolationCode.SOD_VIOLATION,
                        message=(
                            f"Segregation of Duties violation: Preparer ({self_approving}) "
                            f"cannot approve their own proposal ({approver_id})."
                        ),
                        field="reviewed_by",
                        details={"preparer_id": self_approving, "approver_id": approver_id},
                    )
                )
            elif not server_preparer_id:
                violations.append(
                    InvariantViolation(
                        code=InvariantViolationCode.PREPARER_PRINCIPAL_UNVERIFIED,
                        message=(
                            "The preparer has no server-established identity, so preparer != "
                            f"approver ('{approver_id}') cannot be shown."
                        ),
                        field="created_by",
                        details={"approver_id": approver_id, "claimed_preparer_id": claimed_preparer_id or None},
                    )
                )

            # Only a declared human may approve; automated principals never can.
            if approver_type in (PrincipalType.AI_AGENT, PrincipalType.SYSTEM):
                violations.append(
                    InvariantViolation(
                        code=InvariantViolationCode.AI_SELF_APPROVAL,
                        message=(
                            f"AI agents cannot approve financial proposals. "
                            f"Approver '{approver_id}' is not an authorized human reviewer."
                        ),
                        field="reviewed_by",
                        details={"approver_id": approver_id, "approver_principal_type": approver_type.value},
                    )
                )
            elif approver_type is None:
                violations.append(
                    InvariantViolation(
                        code=InvariantViolationCode.APPROVER_PRINCIPAL_UNVERIFIED,
                        message=(
                            f"Approver '{approver_id}' has no server-established principal type; "
                            "an approval is accepted only from a principal declared human."
                        ),
                        field="reviewed_by",
                        details={"approver_id": approver_id},
                    )
                )

        metadata = {
            "total_debits": str(total_debits),
            "total_credits": str(total_credits),
            "difference": str(diff),
            "balanced": not invalid_amounts_found and diff < self.materiality_tolerance,
            "line_count": len(lines),
            "entry_date": entry_date.isoformat() if entry_date else None,
            "preparer_principal_type": preparer_type.value if preparer_type else None,
        }

        has_critical = any(v.severity == InvariantSeverity.CRITICAL for v in violations)
        return RailValidationResult(
            passed=not has_critical,
            violations=violations,
            metadata=metadata,
        )

    def validate_action(
        self,
        *,
        action_type: str,
        payload: Dict[str, Any],
        context: Optional[Dict[str, Any]] = None,
    ) -> RailValidationResult:
        """Validate any proposed action before writing to ledger."""
        ctx = context or {}
        if not isinstance(payload, dict):
            return RailValidationResult(
                passed=False,
                violations=[
                    InvariantViolation(
                        code=InvariantViolationCode.INVALID_PAYLOAD_STRUCTURE,
                        message="Payload must be a dictionary.",
                    )
                ],
            )

        # Direct-Write Gate: Direct execution flag prohibited
        if payload.get("direct_execute") is True or ctx.get("bypass_approval") is True:
            return RailValidationResult(
                passed=False,
                violations=[
                    InvariantViolation(
                        code=InvariantViolationCode.DIRECT_WRITE_ATTEMPT,
                        message="Direct ledger writes are strictly prohibited. Action must stage through ProposedAction.",
                        field="direct_execute",
                    )
                ],
            )

        norm_action = str(action_type).upper()
        if norm_action in ("JOURNAL_ENTRY", "RECLASSIFICATION", "ELIMINATION"):
            return self.validate_journal_entry_proposal(payload, context=ctx)

        # No rails exist for other action types (e.g. VENDOR_UPDATE, REFORECAST):
        # report that explicitly instead of a pass nothing was checked for.
        return RailValidationResult(
            passed=False,
            violations=[
                InvariantViolation(
                    code=InvariantViolationCode.UNSUPPORTED_ACTION_TYPE,
                    message=f"No invariant rails exist for action type '{norm_action}'; it was not validated.",
                    field="action_type",
                )
            ],
            metadata={"action_type": norm_action, "evaluated": False},
        )

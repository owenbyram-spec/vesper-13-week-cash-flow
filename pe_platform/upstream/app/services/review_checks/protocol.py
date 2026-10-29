"""The analytical-review check framework — Protocol, Finding, run_check.

Phase C C0 (plan 20260620-phase-c-analytical-review-build, AC-1). This module
LIFTS the running 5-stage proposal-first pattern in ``_propose_missing_accruals``
(app/tasks/proactive.py:226-292) into a named contract two callers share — the
nightly proactive run and the Phase B close driver (close_orchestration.py:249).
The on-disk sequence it generalizes (proactive.py:226-292):

    detect (ZERO LLM, :242)  →  if not hit: return 0 (:243, clean→no LLM)
    →  hard-lock gate (period_is_hard_locked, D-B6-1, FAIL-CLOSED)
    →  materiality gate (:266)  →  cap (:264)
    →  exactly ONE LLM draft per material item (:268)
    →  PENDING ProposedAction (:270-287, proposal-first; NO GL write)

PROPOSAL-FIRST (CLAUDE.md §5, agentic-writeback Control 1): ``run_check`` NEVER
writes the GL. It drafts a PENDING ProposedAction via the injected ``propose``
callable — a human still approves every write. A direct GL write here is a defect.

Two-layer type relationship (CRITICAL-REVIEW §2.4 condition 4):
  - ``Finding`` (INNER, content-level) — what a check's ``scan`` returns: the
    Hit-vs-could_not_verify CONTENT outcome + the ``verifiable`` discriminator.
  - ``CheckResult`` (OUTER, run-level) — what ``run_check`` returns: the RUN
    outcome (clean / period_locked / blocked / drafted / could_not_verify /
    error) a single Finding cannot carry, plus the Finding and proposed count.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any, Protocol, runtime_checkable  # CRAFT-OK: Any only at the dynamic qbo_client / llm-service / JSON-payload edges, each tagged below

from sqlalchemy.orm import Session

from ..llm_task_context import CloseBudgetExceededError  # PLAN-EXEMPT: ratified plan 20260702-qa-remediation W-1
from .materiality import Materiality

logger = logging.getLogger(__name__)

# OUR-defect types the LLM layer deliberately re-raises instead of degrading to a
# second billable fallback call (llm.py predict_text_result). Mirrored here so the
# same bad kwarg / missing attribute / unresolved name / missing SDK is not
# re-laundered one level up into a deterministic-message proposal.
PROGRAMMING_ERRORS = (TypeError, AttributeError, NameError, ImportError)

# A materiality-gate input: a money/ratio scalar or absent. Modeled (not Any) so
# the materiality coercion has a real contract; Materiality._to_decimal narrows it.
Numeric = Decimal | int | float | str

# A detector / proposal JSON payload — the unmodeled provider-shaped dict that
# becomes ProposedAction.payload (models.py:1064 Column(JSON)). The check authors
# in C1/C2 own each concrete shape; at the framework edge it is the JSON boundary.
JsonPayload = dict[str, Any]  # CRAFT-OK: dynamic provider/detector JSON payload boundary → ProposedAction.payload Column(JSON)


# ─────────────────────────────────────────────────────────────────────────────
# Finding — the INNER, content-level type (status × verifiable, ONE type)
# ─────────────────────────────────────────────────────────────────────────────


class FindingOutcome(str, Enum):
    """The content outcome of a check's deterministic scan (STAGE 1).

    - HIT:              the check found a material exception worth proposing.
    - NO_HIT:           the check ran and found nothing — clean books.
    - COULD_NOT_VERIFY: the check could NOT evaluate (input absent / detector
                        no-data / a degraded source). FIRST-CLASS, never folded
                        into NO_HIT — a check that cannot see the data has NOT
                        proven the books clean (DEC-CAR-C-4, SYNTHESIS line 23).
    """

    HIT = "hit"
    NO_HIT = "no_hit"
    COULD_NOT_VERIFY = "could_not_verify"


class FindingSeverity(str, Enum):
    """Severity of a finding. Note the invariant tying it to ``outcome`` below."""

    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass
class Finding:
    """ONE finding type — the status × verifiable relationship, made EXPLICIT.

    The relationship (steelman of CRITICAL-REVIEW weak-spot #4 / cond4):

      * ``verifiable: bool`` is the load-bearing discriminator. ``verifiable is
        False`` means the check could NOT evaluate — equivalent to
        ``outcome == COULD_NOT_VERIFY``. This is a FIRST-CLASS state, never a
        silent clean pass (DEC-CAR-C-4 bans ``recon_break = x or Decimal("0")``).

      * ``outcome`` is the 3-value content status. The INVARIANT this type
        enforces in ``__post_init__`` (refuse > fabricate):
          - ``outcome == COULD_NOT_VERIFY``  ⇔  ``verifiable is False``.
            (They are the SAME fact stated two ways; they may not disagree.)
          - ``could_not_verify`` is NEVER a clean pass: its ``severity`` may not
            be ``INFO`` (a check that could not see the data must surface, not
            whisper). A could_not_verify finding defaults to ``WARNING``.
          - ``NO_HIT`` (clean) and ``HIT`` (exception) are BOTH ``verifiable is
            True`` — the check ran and reached a real conclusion.

    A check builds a Finding via the constructors ``hit`` / ``no_hit`` /
    ``could_not_verify`` so the invariant is satisfied by construction.

    ``amount`` / ``pct`` feed the materiality gate (Materiality.is_material).
    ``payload`` carries the deterministic detector output that becomes the
    ProposedAction.payload on a drafted proposal. ``message`` rides the Phase B
    finding seam (close_orchestration verification_message).
    """

    check_id: str
    outcome: FindingOutcome
    verifiable: bool
    severity: FindingSeverity = FindingSeverity.INFO
    message: str = ""
    amount: Numeric | None = None
    pct: Numeric | None = None
    payload: JsonPayload = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Invariant 1: could_not_verify ⇔ not verifiable (same fact, two views).
        if (self.outcome is FindingOutcome.COULD_NOT_VERIFY) != (not self.verifiable):
            raise ValueError(
                f"Finding invariant violated for {self.check_id!r}: "
                f"outcome={self.outcome.value} but verifiable={self.verifiable}. "
                "could_not_verify ⇔ verifiable is False — they may not disagree."
            )
        # Invariant 2: could_not_verify is NEVER a clean pass (no INFO severity).
        if self.outcome is FindingOutcome.COULD_NOT_VERIFY and self.severity is FindingSeverity.INFO:
            self.severity = FindingSeverity.WARNING

    # ── construction-time-safe builders (satisfy the invariant by design) ────
    @classmethod
    def hit(
        cls,
        check_id: str,
        *,
        message: str,
        severity: FindingSeverity = FindingSeverity.WARNING,
        amount: Numeric | None = None,
        pct: Numeric | None = None,
        payload: JsonPayload | None = None,
    ) -> "Finding":
        return cls(
            check_id=check_id,
            outcome=FindingOutcome.HIT,
            verifiable=True,
            severity=severity,
            message=message,
            amount=amount,
            pct=pct,
            payload=payload or {},
        )

    @classmethod
    def no_hit(cls, check_id: str, *, message: str = "") -> "Finding":
        """A clean, VERIFIED pass — the check ran and found nothing."""
        return cls(
            check_id=check_id,
            outcome=FindingOutcome.NO_HIT,
            verifiable=True,
            severity=FindingSeverity.INFO,
            message=message,
        )

    @classmethod
    def could_not_verify(
        cls,
        check_id: str,
        *,
        message: str,
        severity: FindingSeverity = FindingSeverity.WARNING,
    ) -> "Finding":
        """The honest-degradation path (DEC-CAR-C-4): the check could NOT evaluate.

        NOT a clean pass. Emit this — never ``no_hit`` and never a fabricated
        ``Decimal("0")`` — when the detector returns no data or an input is absent.
        """
        return cls(
            check_id=check_id,
            outcome=FindingOutcome.COULD_NOT_VERIFY,
            verifiable=False,
            severity=severity,
            message=message,
        )


# ─────────────────────────────────────────────────────────────────────────────
# CheckContext — what a check needs (generalizes proactive.py's passed args)
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class CheckContext:
    """Everything a check + run_check need for one (realm, period) evaluation.

    Generalizes what ``_propose_missing_accruals`` passes around (proactive.py:226):
    ``db`` + ``company_id`` (realm_id) + ``period`` + the LLM ``service`` + the
    ProposedAction writer. The Phase B close driver passes ``qbo_client=None``
    today (close_orchestration.py) — so a check whose detector needs a live QBO
    client must emit ``Finding.could_not_verify`` when ``qbo_client is None``
    rather than crash (honest degradation).

    ``propose`` is the INJECTED ProposedAction writer (proposal-first seam): the
    nightly run and the close driver inject different writers (different dedupe
    key / close_task_id linkage) but share the same Check. ``run_check`` calls
    ``ctx.propose(...)`` — it NEVER constructs or commits a GL write itself.

    ``materiality`` is the per-check threshold injected at registration (Phase E
    hook) — NOT hardcoded in ``scan``.
    """

    db: Session
    realm_id: str
    period: str
    propose: "ProposeCallable"
    materiality: Materiality
    qbo_client: Any | None = None  # CRAFT-OK: hand-rolled opaque QBO REST client (no SDK / no type stubs)
    company_name: str = ""
    llm: Any | None = None  # CRAFT-OK: dynamic LaneLLMService injected for test seam / lane choice
    # Phase B close-orchestration linkage (None on the nightly proactive path).
    close_task_id: int | None = None
    agent_run_id: int | None = None
    step_index: int | None = None
    extra: JsonPayload = field(default_factory=dict)


class ProposeCallable(Protocol):
    """The injected PENDING-ProposedAction writer (proposal-first).

    An implementation drafts a ``ProposedAction(status="PENDING")`` and returns
    its id (or None if deduped to an existing proposal). It NEVER posts to the GL.
    The nightly run and the close driver supply different implementations differing
    only by dedupe key + close-task linkage (SYNTHESIS line 53).
    """

    def __call__(
        self,
        *,
        ctx: "CheckContext",
        finding: Finding,
        reasoning: str,
    ) -> int | None: ...


# ─────────────────────────────────────────────────────────────────────────────
# Check Protocol — the check owns STAGE 1 (deterministic detect + classify)
# ─────────────────────────────────────────────────────────────────────────────


@runtime_checkable
class Check(Protocol):
    """A single analytical-review check.

    ``scan`` owns STAGE 1 ONLY: the deterministic detector call (a pure
    ``app/analysis`` ``f(qbo_client, ...) -> dict`` with built-in no-data
    degradation) + classification into a ``Finding``. It does the ZERO-LLM work
    and emits ``Finding.could_not_verify`` on a degraded/absent source. It does
    NOT run the materiality / closed-period / cap / LLM / propose gates — those
    are ``run_check``'s job, run identically for every check (the generalization).
    """

    check_id: str

    def scan(self, ctx: CheckContext) -> Finding: ...


# ─────────────────────────────────────────────────────────────────────────────
# CheckResult — the OUTER, run-level type (status a single Finding can't carry)
# ─────────────────────────────────────────────────────────────────────────────


class CheckRunStatus(str, Enum):
    """The run-level outcome of ``run_check`` (CRITICAL-REVIEW §2.4 cond4).

    Distinct from ``FindingOutcome`` (content): these are how the RUN ended.
    """

    CLEAN = "clean"  # scan returned NO_HIT — clean books, ZERO LLM, no proposal
    PERIOD_LOCKED = "period_locked"  # closed-period gate fired (fail-closed)
    BLOCKED = "blocked"  # tenant-budget hard block stopped LLM drafting
    DRAFTED = "drafted"  # material HIT → ONE LLM draft → PENDING proposal
    COULD_NOT_VERIFY = "could_not_verify"  # scan could not evaluate (degraded)
    IMMATERIAL = "immaterial"  # HIT but below the materiality floor — no proposal
    ERROR = "error"  # an unexpected error — fail-closed, no proposal


@dataclass
class CheckResult:
    """What ``run_check`` returns — the outer run-level result.

    ``finding`` is the inner content-level Finding (None only on a pre-scan
    error). ``proposed_action_id`` is set only when a PENDING proposal was
    drafted (DRAFTED). ``llm_calls`` makes the clean→zero-LLM guarantee testable
    (SYNTHESIS line 22 / AC-5): it is 0 on CLEAN/PERIOD_LOCKED/BLOCKED/IMMATERIAL/
    COULD_NOT_VERIFY and 1 on DRAFTED.
    """

    check_id: str
    status: CheckRunStatus
    finding: Finding | None = None
    proposed_action_id: int | None = None
    llm_calls: int = 0
    detail: str = ""


# ─────────────────────────────────────────────────────────────────────────────
# run_check — the shared gate orchestrator (generalizes proactive.py:226-292)
# ─────────────────────────────────────────────────────────────────────────────


def run_check(db: Session, check: Check, ctx: CheckContext) -> CheckResult:
    """Run ONE check through the proven gate sequence. NEVER writes the GL.

    Generalizes ``_propose_missing_accruals`` (proactive.py:226-292) stage-for-
    stage, in order:

      1. ``check.scan(ctx)`` — deterministic STAGE 1, ZERO LLM (proactive.py:242).
      2. COULD_NOT_VERIFY → return immediately, ZERO LLM (honest degradation;
         a degraded source is surfaced, never treated as a clean pass).
      3. NO_HIT (clean) → return immediately, ZERO LLM (proactive.py:243 — the
         clean-exit-BEFORE-LLM economics; the LLM service is never even obtained).
      4. hard-lock gate (period_is_hard_locked, D-B6-1, FAIL-CLOSED; an
         indeterminate read returns locked, never proposes into a hard-locked period).
      5. materiality gate (ctx.materiality.is_material — proactive.py:266).
      6. cap — caller bounds the number of run_check calls per (realm, period)
         (the proactive.py:264 MAX_*_PER_RUN cap is a loop-level concern the
         caller owns; run_check evaluates ONE candidate, so the cap lives above).
      7. exactly ONE LLM draft (proactive.py:268) — drafts a rationale/memo.
      8. ``ctx.propose(...)`` a PENDING ProposedAction (proactive.py:270-287) —
         the INJECTED writer; proposal-first; NO GL write.

    Fail-closed throughout: any unexpected error returns ERROR with no proposal
    rather than a fabricated pass (DEC-CAR-C-4 / error-logging.md §1/§5).
    """
    # Imported lazily to keep this framework module import-light and avoid a
    # heavy import chain at app boot (period_is_hard_locked pulls the ORM session lib).
    from ..task_autonomy import period_is_hard_locked

    # ── Stage 1: deterministic scan (ZERO LLM) ───────────────────────────────
    try:
        finding = check.scan(ctx)
    except Exception as e:  # fail-CLOSED: a scan crash is not a clean pass.
        from ..redaction import redact_to_string

        logger.error("review-check %s scan failed: %s", check.check_id, redact_to_string(e))
        return CheckResult(
            check_id=check.check_id,
            status=CheckRunStatus.ERROR,
            finding=None,
            llm_calls=0,
            detail="scan raised; failing closed (no proposal)",
        )

    # ── Stage 2: honest degradation — could_not_verify is first-class ────────
    if finding.outcome is FindingOutcome.COULD_NOT_VERIFY:
        return CheckResult(
            check_id=check.check_id,
            status=CheckRunStatus.COULD_NOT_VERIFY,
            finding=finding,
            llm_calls=0,
            detail=finding.message,
        )

    # ── Stage 3: clean books → return BEFORE the LLM (zero-LLM economics) ─────
    if finding.outcome is FindingOutcome.NO_HIT:
        return CheckResult(
            check_id=check.check_id,
            status=CheckRunStatus.CLEAN,
            finding=finding,
            llm_calls=0,
        )

    # ── Stage 4: hard-lock gate (FAIL-CLOSED) ────────────────────────────────
    # Never propose an adjustment into a HARD-locked period (D-B6-1, 2026-07-16 —
    # the write/propose floor is the explicit hard lock; a soft-CLOSED auto-rolled
    # month is books-rolled, not write-blocked). An indeterminate read returns True
    # (locked) by period_is_hard_locked's own contract.
    if period_is_hard_locked(db, realm_id=ctx.realm_id, period=ctx.period):
        return CheckResult(
            check_id=check.check_id,
            status=CheckRunStatus.PERIOD_LOCKED,
            finding=finding,
            llm_calls=0,
            detail=f"period {ctx.period} is locked; no proposal",
        )

    # ── Stage 5: materiality gate (per-check, injectable — Phase E hook) ──────
    if not ctx.materiality.is_material(amount=finding.amount, pct=finding.pct):
        return CheckResult(
            check_id=check.check_id,
            status=CheckRunStatus.IMMATERIAL,
            finding=finding,
            llm_calls=0,
            detail="below materiality floor; no proposal",
        )

    # ── Stage 7: exactly ONE LLM draft (proactive.py:268) ────────────────────
    # The single escalation per material exception. A draft FAILURE is non-fatal:
    # we still propose with the deterministic message (proactive.py:269 pattern).
    reasoning = finding.message
    llm_calls = 0
    try:
        drafted = _draft_one(ctx, finding)
        llm_calls = 1
        if drafted:
            reasoning = drafted
    except CloseBudgetExceededError:
        # PLAN-EXEMPT: ratified plan 20260702-qa-remediation W-1 (F-1).
        # Budget exhaustion is FAIL-LOUD by contract: never degrade it into the
        # "propose with the deterministic message" path below — the close walk
        # converts it into a failed run + WatchdogAlert + finding row.
        raise
    except PROGRAMMING_ERRORS:
        # FAIL-LOUD on OUR defects, matching llm.py's re-raise guard: swallowing a
        # code bug here would still report a drafted proposal — the exact fail-quiet
        # the LLM-layer re-raise exists to end. Provider/transport failures fall through.
        raise
    except Exception as e:  # draft failure → fall back to the deterministic msg.
        from ..redaction import redact_to_string

        logger.warning(
            "review-check %s LLM draft failed; proposing with deterministic message: %s",
            check.check_id,
            redact_to_string(e),
        )

    # ── Stage 8: propose a PENDING ProposedAction (proposal-first; NO GL) ─────
    proposed_id = ctx.propose(ctx=ctx, finding=finding, reasoning=reasoning)
    return CheckResult(
        check_id=check.check_id,
        status=CheckRunStatus.DRAFTED,
        finding=finding,
        proposed_action_id=proposed_id,
        llm_calls=llm_calls,
        detail=reasoning,
    )


def _draft_one(ctx: CheckContext, finding: Finding) -> str:
    """The single LLM drafting call (proactive.py:204-223 _draft_accrual_proposal).

    Drafts a concise rationale/memo for the proposed adjustment. Makes NO mutating
    call — the QBO write only happens later, on human approval (proposal-first).
    Returns "" if no LLM service is available or it produced no text.
    """
    service = ctx.llm
    if service is None:
        from ..llm import get_llm_service

        service = get_llm_service("analysis")

    result = service.predict_text_result(
        system_prompt=(
            "You draft a concise rationale and a GL memo for a proposed adjusting "
            "entry surfaced by an analytical-review check. Be brief and specific."
        ),
        user_prompt=(
            f"Company: {ctx.company_name or ctx.realm_id}. Period: {ctx.period}. "
            f"Check: {finding.check_id}. Finding: {finding.message}. "
            "Draft a one-paragraph rationale and a GL memo for the adjusting entry."
        ),
        temperature=0.2,
        model=None,
        json_mode=False,
        allow_fallback=True,
    )
    text = getattr(result, "text", None) or getattr(result, "content", None) or ""
    return str(text) if text else ""

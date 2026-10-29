"""Materiality — the per-check, config-injectable materiality value object.

Phase C (DEC-CAR-C-3, SYNTHESIS §Area 3 / line 59). Eight inline materiality
floors live on disk today (accrual $100 proactive.py:179, GL-scrape $500 :315,
detector-confidence 0.65 :345, runway <3 :579, AR ratio 0.20 :616, revenue-drop
<-15 :596, deal-variance <-10 :684, budget DUAL ``pct>10 AND abs>=1000`` :764).
This object generalizes those THREE on-disk shapes — absolute floor, percent /
ratio, derived — plus the DUAL floor (a percent AND an absolute, the budget shape
at proactive.py:764) — into ONE value object a Check carries.

The KEY property (DEC-CAR-C-3, the Phase E hook): materiality is set per-check at
REGISTRATION (``register_check(..., materiality=...)``), never hardcoded inside a
check's ``scan``. Phase C ships the on-disk default per check; Phase E supplies
per-company values by overriding the registered object — with ZERO check-code
change. A check that hardcodes ``if amount < Decimal("100")`` inside ``scan``
defeats this hook; the floor MUST flow in through ``ctx.materiality``.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum


def _to_decimal(value: object) -> Decimal | None:
    """Coerce a numeric-ish value to Decimal; None on absent/uncoercible.

    Returns None (not 0) for None/uncoercible input — a fabricated ``Decimal("0")``
    on a missing value is the banned silent-pass anti-pattern (DEC-CAR-C-4); the
    caller must treat None as 'unknown / could-not-evaluate', never as 'immaterial'.
    """
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


class MaterialityShape(str, Enum):
    """The three on-disk shapes + the dual floor, made first-class.

    - ABSOLUTE: a raw amount floor (accrual $100, GL-scrape $500).
    - PERCENT: a ratio / percent floor (AR ratio 0.20, revenue-drop -15%,
      deal-variance -10%, detector-confidence 0.65).
    - DERIVED: a derived-quantity floor (runway < 3 periods) — same machinery as
      ABSOLUTE, named distinctly so a check declares the quantity it gates on.
    - DUAL: BOTH a percent AND an absolute must clear (budget ``pct>10 AND
      abs>=1000``, proactive.py:764) — the adversarial single-gate mutant catcher.
    """

    ABSOLUTE = "absolute"
    PERCENT = "percent"
    DERIVED = "derived"
    DUAL = "dual"


@dataclass(frozen=True)
class Materiality:
    """Immutable per-check materiality threshold.

    Construct via the classmethods (they name the shape) rather than the raw
    constructor, e.g. ``Materiality.absolute(Decimal("100"))`` (accrual floor),
    ``Materiality.dual(pct=Decimal("10"), abs_floor=Decimal("1000"))`` (budget).

    ``is_material(amount=..., pct=...)`` is the gate ``run_check`` consults: it
    returns True only when the configured threshold(s) clear. A None value for a
    required side returns False — below materiality, NOT a fabricated pass; the
    could_not_verify path is the Finding's job, not the materiality gate's.
    """

    shape: MaterialityShape
    abs_floor: Decimal | None = None
    pct_floor: Decimal | None = None
    label: str = ""

    # ── named constructors (one per on-disk shape) ───────────────────────────
    @classmethod
    def absolute(cls, abs_floor: Decimal, *, label: str = "") -> "Materiality":
        return cls(shape=MaterialityShape.ABSOLUTE, abs_floor=Decimal(str(abs_floor)), label=label)

    @classmethod
    def percent(cls, pct_floor: Decimal, *, label: str = "") -> "Materiality":
        return cls(shape=MaterialityShape.PERCENT, pct_floor=Decimal(str(pct_floor)), label=label)

    @classmethod
    def derived(cls, abs_floor: Decimal, *, label: str = "") -> "Materiality":
        return cls(shape=MaterialityShape.DERIVED, abs_floor=Decimal(str(abs_floor)), label=label)

    @classmethod
    def dual(cls, *, pct: Decimal, abs_floor: Decimal, label: str = "") -> "Materiality":
        """The budget DUAL floor (proactive.py:764): pct AND abs must BOTH clear."""
        return cls(
            shape=MaterialityShape.DUAL,
            pct_floor=Decimal(str(pct)),
            abs_floor=Decimal(str(abs_floor)),
            label=label,
        )

    # ── the gate run_check consults ──────────────────────────────────────────
    def is_material(
        self,
        *,
        amount: object = None,
        pct: object = None,
    ) -> bool:
        """Whether the candidate clears this check's materiality floor.

        Generalizes the on-disk gates:
          ABSOLUTE/DERIVED → ``amount >= abs_floor`` (accrual :266, runway :579).
          PERCENT          → ``abs(pct) >= pct_floor`` (two-sided; AR ratio :616).
          DUAL             → ``abs(pct) >= pct_floor AND amount >= abs_floor``
                             (budget :764 — the single-gate mutant catcher).

        A None on a side this shape requires → False (immaterial, never a
        fabricated pass). Two-sided ``abs`` on the percent so a NEW two-sided
        check (DEC-CAR-C-7) is not forced one-directional by the gate.
        """
        amt = _to_decimal(amount)
        pc = _to_decimal(pct)

        if self.shape in (MaterialityShape.ABSOLUTE, MaterialityShape.DERIVED):
            if amt is None or self.abs_floor is None:
                return False
            return amt >= self.abs_floor

        if self.shape is MaterialityShape.PERCENT:
            if pc is None or self.pct_floor is None:
                return False
            return abs(pc) >= self.pct_floor

        if self.shape is MaterialityShape.DUAL:
            if amt is None or pc is None or self.abs_floor is None or self.pct_floor is None:
                return False
            return abs(pc) >= self.pct_floor and amt >= self.abs_floor

        return False  # unreachable: enum-exhaustive above

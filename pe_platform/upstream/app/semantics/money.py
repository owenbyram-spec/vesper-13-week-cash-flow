"""
Money semantics — the repo's Decimal money policy for NEW code (FOS-1, Wave A).

THE POLICY (financial-modeling-correctness): **recon / JE / claims paths are
`Decimal` end-to-end; display/analysis edges may float at the boundary via
`to_display_float`** — the ONE sanctioned Decimal→float exit. A float sum of
currency amounts drifts by fractions of a cent and makes a genuinely-balanced
figure read as out-of-balance (or vice versa); the drift is invisible in a unit
test that asserts the same buggy float arithmetic back at itself.

This module CODIFIES the discipline already on disk — it does not invent a new
one:

- ``TWO_PLACES`` / ``quantize(TWO_PLACES)`` — the JE builder's cent quantum
  (app/services/accounting/je_builder.py:201, ``build_disposal_je``).
- ``ZERO`` (``Decimal("0.00")``) — the recon tie-out's ``_ZERO`` quantum
  (app/services/bank_recon/session.py:52): comparisons and DTO values are
  quantized so ``Decimal("0")`` and ``Decimal("0.00")`` compare at cent
  resolution.
- **None-not-0** — a fabricated ``Decimal("0")`` on a missing/uncoercible value
  is the banned silent-pass anti-pattern (DEC-CAR-C-4,
  app/services/review_checks/materiality.py:_to_decimal): ``to_money`` returns
  ``None`` for absent/invalid input, NEVER a silent zero the caller would read
  as "immaterial" / "balanced".
- Paren-negative + comma-grouped parsing — the QBO report-cell conventions of
  app/budget/qbo_reports.py:_parse_amount (``"(1,234.56)"`` is −1234.56),
  minus its float return and its invalid→0.0 fabrication.

Quantization policy: money is quantized to 2dp with ``ROUND_HALF_UP`` (ties
away from zero — the bookkeeping convention); ratios/percentages to 4dp.
Accumulate EXACT Decimals and quantize ONCE at the end (``sum_money``) — never
per-element, which compounds rounding error (two 0.005s must sum to 0.01, not
0.02).

Float caveat (documented per the policy): a float that should sit exactly on a
half-cent boundary (2.005) is NOT reliably representable in binary —
``to_money`` converts floats via ``str()`` (shortest round-trip repr), so a
*literal* ``2.005`` parses as "2.005" and rounds up to 2.01, but a float that
*arrives* near a half-cent through arithmetic may fall on either side of the
tie. Boundary-exact amounts must originate as ``str`` or ``Decimal``, never
``float``.
"""
from __future__ import annotations

import math
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Iterable

# Money quantum: cent precision, matching the Numeric(18, 2) model columns
# (SQLAlchemy returns those as Decimal) and je_builder.TWO_PLACES.
TWO_PLACES = Decimal("0.01")

# The canonical money zero, at cent resolution (bank_recon/session.py _ZERO).
ZERO = Decimal("0.00")

# Ratio quantum: 4dp for rates / percentages / ratios (not currency amounts).
FOUR_PLACES = Decimal("0.0001")


def to_money(value: str | int | float | Decimal | None) -> Decimal | None:
    """Coerce a money-ish value to a cent-quantized ``Decimal``; ``None`` if absent/invalid.

    None-in-None-out; invalid input returns ``None``, NEVER a fabricated 0 (the
    caller must treat ``None`` as unknown, not as zero — DEC-CAR-C-4).

    Accepted inputs and their handling:

    - ``Decimal`` — quantized to 2dp (non-finite → ``None``).
    - ``int`` — exact; ``bool`` is REJECTED (``True`` silently becoming 1.00
      would mask a type error upstream).
    - ``float`` — converted via ``str()`` (shortest round-trip repr) to avoid
      binary artifacts (``0.1 + 0.2`` → ``0.30``, not 0.30000000000000004…);
      NaN/inf → ``None``. See the module-docstring float caveat for half-cent
      boundary values.
    - ``str`` — the _parse_amount conventions (app/budget/qbo_reports.py):
      whitespace stripped, thousands commas removed, and a parenthesized value
      ``"(1,234.56)"`` read as negative. Empty/unparseable → ``None``.

    The result is ALWAYS quantized to ``TWO_PLACES`` with ``ROUND_HALF_UP`` —
    ``to_money`` is the parse-and-normalize gate for a single amount. To
    accumulate many amounts, sum the exact inputs with ``sum_money`` instead of
    summing per-quantized ``to_money`` outputs.
    """
    if value is None:
        return None

    if isinstance(value, bool):  # bool is an int subclass — a bool is never money
        return None

    if isinstance(value, Decimal):
        if not value.is_finite():
            return None
        return value.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)

    if isinstance(value, int):
        return Decimal(value).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)

    if isinstance(value, float):
        # str() (repr) round-trips the shortest decimal literal — Decimal(str(x)),
        # never Decimal(x), which would drag in the full binary expansion.
        if not math.isfinite(value):  # NaN / ±inf
            return None
        return Decimal(str(value)).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)

    if isinstance(value, str):
        s = value.replace(",", "").strip()
        if s.startswith("(") and s.endswith(")"):
            s = "-" + s[1:-1]
        if not s:
            return None
        try:
            d = Decimal(s)
        except (InvalidOperation, ValueError):
            return None
        if not d.is_finite():  # Decimal("NaN") / Decimal("inf") parse — reject
            return None
        return d.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)

    return None  # unsupported type — unknown, not zero


def quantize_money(d: Decimal, rounding: str = ROUND_HALF_UP) -> Decimal:
    """Quantize a ``Decimal`` amount to cents (2dp), ``ROUND_HALF_UP`` by default.

    The one boundary where a computed money value is rounded — apply it ONCE,
    at the end of a computation (see ``sum_money``), not per intermediate step.
    """
    return d.quantize(TWO_PLACES, rounding=rounding)


def quantize_ratio(d: Decimal) -> Decimal:
    """Quantize a rate/percentage/ratio ``Decimal`` to 4dp, ``ROUND_HALF_UP``.

    Ratios are not currency: 2dp on a rate (0.0834 → 0.08) destroys the signal.
    """
    return d.quantize(FOUR_PLACES, rounding=ROUND_HALF_UP)


def sum_money(amounts: Iterable[Decimal | None]) -> Decimal:
    """Sum ``Decimal`` amounts exactly; skip ``None``; quantize ONCE at the end.

    Accumulation is exact Decimal arithmetic — no per-element quantization
    (two ``Decimal("0.005")`` elements sum to ``0.01``, not the per-element
    half-up ``0.02``). ``None`` elements are SKIPPED, not zero-coerced: in a
    sum the additive identity is legitimate, but whether an all-``None`` input
    means "0.00" or "unknown" is the CALLER's judgment — check for that case
    before calling if it matters. An empty/all-``None`` input returns ``ZERO``.

    Elements must already be ``Decimal`` (e.g. straight off Numeric(18, 2)
    columns, or via ``to_money``). A stray ``float`` fails LOUD (``TypeError``
    from ``Decimal + float``) — never silently contaminates the sum.
    """
    total = Decimal("0")
    for amount in amounts:
        if amount is None:
            continue
        total += amount
    return total.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def to_display_float(d: Decimal | None) -> float | None:
    """The ONE sanctioned Decimal→float exit, for display/analysis edges only.

    None-in-None-out. Use at the boundary where a serializer, chart, or
    analysis routine genuinely needs a float — NEVER feed the result back into
    money arithmetic (recon / JE / claims paths stay Decimal end-to-end).
    """
    if d is None:
        return None
    return float(d)

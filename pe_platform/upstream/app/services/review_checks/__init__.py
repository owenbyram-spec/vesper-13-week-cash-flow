"""review_checks — the analytical-review check framework (Phase C).

Public surface for the check library that generalizes the proven proposal-first
pattern (proactive.py:226-292) into a pluggable Protocol. C0 (this chunk) ships
the FRAMEWORK; C1/C2 (next wave) populate the registries with concrete checks.

The two registries (SYNTHESIS §Proposed-pattern item 1):
  - ``CHECK_REGISTRY``     : check_id            -> the Check instance.
  - ``TASK_TYPE_TO_CHECK`` : close-task task_type -> check_id.
        The parallel to ``close_orchestration.TASK_TYPE_TO_TOOL`` (:84) — it lets
        the Phase B close driver resolve a close task's task_type to the Check
        that reviews it. The SAME Check also runs in the nightly proactive loop;
        the two callers differ only by the injected ``propose`` (CheckContext).

``register_check`` is the single registration seam. It binds a Check to its
per-check ``Materiality`` (the Phase E hook: the default ships here in C1/C2, Phase
E overrides it per company at registration — ZERO check-code change), and
optionally maps one or more close-task task_types to it.
"""

from __future__ import annotations

from enum import Enum

from .materiality import Materiality, MaterialityShape
from .protocol import (
    Check,
    CheckContext,
    CheckResult,
    CheckRunStatus,
    Finding,
    FindingOutcome,
    FindingSeverity,
    ProposeCallable,
    run_check,
)


# ─────────────────────────────────────────────────────────────────────────────
# CheckTier — the aggressiveness skip signal (Phase E AC-1; CRITICAL-REVIEW §2.1)
# ─────────────────────────────────────────────────────────────────────────────
# A Check carries NO severity on disk (protocol.py:262 — only check_id + scan;
# FindingSeverity is a *Finding* attribute known only post-scan). So the
# whole-check skip lever has no pre-scan signal from the Check itself — the
# CRITICAL-REVIEW close-path is an additive ``register_check(tier=...)`` kwarg.
# The tier is the static, pre-scan risk-class the company's review_aggressiveness
# gates against (see check_runs_under). DEFAULT is STANDARD so an un-tiered
# legacy registration is unchanged under the default (balanced) aggressiveness.


class CheckTier(str, Enum):
    """The static risk-class a check is registered at (pre-scan; AC-1).

    The company's ``review_aggressiveness`` (conservative | balanced | aggressive)
    selects which tiers run (``check_runs_under``):

      - CRITICAL: load-bearing checks (subledger↔GL recon, covenant projection) —
        run at EVERY aggressiveness, including conservative.
      - STANDARD: the default tier — runs at balanced + aggressive (the bulk of
        the library); skipped only under conservative.
      - OPTIONAL: nice-to-have checks — run only under aggressive. (No built-in
        check ships OPTIONAL today, so balanced runs every existing check →
        ZERO default regression.)
    """

    CRITICAL = "critical"
    STANDARD = "standard"
    OPTIONAL = "optional"

# ─────────────────────────────────────────────────────────────────────────────
# The two registries (C1/C2 populate them via register_check)
# ─────────────────────────────────────────────────────────────────────────────

CHECK_REGISTRY: dict[str, Check] = {}
TASK_TYPE_TO_CHECK: dict[str, str] = {}

# The per-check materiality, bound at registration (Phase E override point). Kept
# alongside CHECK_REGISTRY so a caller can build a CheckContext with the registered
# default; Phase E swaps the value here per company without touching check code.
CHECK_MATERIALITY: dict[str, Materiality] = {}

# The per-check static risk TIER, bound at registration (Phase E aggressiveness
# skip seam). Read-only at request time by check_runs_under; the per-company
# aggressiveness is read from ReviewPolicy, not stored here (this map is the
# worker-shared static class, identical for every tenant).
CHECK_TIER: dict[str, "CheckTier"] = {}


def register_check(
    check: Check,
    *,
    materiality: Materiality,
    task_types: list[str] | None = None,
    tier: "CheckTier" = CheckTier.STANDARD,
) -> Check:
    """Register a Check + its per-check materiality + tier + (optional) task_type maps.

    Single registration seam (the Phase E hooks live here, not in ``scan``):
      - records ``check`` under its ``check_id`` in ``CHECK_REGISTRY``;
      - records its ``materiality`` (the on-disk default in C1/C2; Phase E
        overrides per company) in ``CHECK_MATERIALITY``;
      - records its static ``tier`` (default STANDARD) in ``CHECK_TIER`` — the
        pre-scan signal the per-company ``review_aggressiveness`` gates against
        (``check_runs_under``). Default STANDARD so an un-tiered registration is
        unchanged under balanced (the default aggressiveness);
      - maps each close-task ``task_type`` to ``check.check_id`` in
        ``TASK_TYPE_TO_CHECK`` (the close-driver resolution seam).

    Idempotent re-registration replaces; a task_type may map to only ONE check
    (a second mapping for the same task_type raises — no silent overwrite).
    """
    check_id = check.check_id
    CHECK_REGISTRY[check_id] = check
    CHECK_MATERIALITY[check_id] = materiality
    CHECK_TIER[check_id] = tier
    for task_type in task_types or []:
        existing = TASK_TYPE_TO_CHECK.get(task_type)  # SA-OK: bounded-loop in-memory dict lookup over a check's few task_types, not a DB query
        if existing is not None and existing != check_id:
            raise ValueError(
                f"task_type {task_type!r} already maps to check {existing!r}; "
                f"refusing to remap to {check_id!r} (one task_type → one check)."
            )
        TASK_TYPE_TO_CHECK[task_type] = check_id
    return check


# ─────────────────────────────────────────────────────────────────────────────
# Activate registration (C3): importing the check modules fires their module-level
# register_check(...) side-effects, so CHECK_REGISTRY / TASK_TYPE_TO_CHECK /
# CHECK_MATERIALITY are populated the moment this package is imported. Placed AFTER
# register_check is defined (the modules import it from here) — no import cycle:
# the checks lazy-import their detectors inside scan(), so importing them here pulls
# in only the framework types, not the heavy app.analysis / app.tasks graph.
# PLAN-EXEMPT: ratified plan 20260706-cfo-grade-financial-workflows chunk C2C-CLOSE owns review_checks/** (Appendix A); active-plan.json glob string is malformed (inline comment), scope itself is ratified.
from . import builtin_checks, new_checks  # noqa: E402,F401 — module-level register_check side-effects populate the registries on package import
from . import cutoff_check, flux_check, rollforward_coverage  # noqa: E402,F401 — C2C-CLOSE checks (flux / cut-off / roll-forward coverage) register on package import
# PLAN-EXEMPT: ratified plan 20260713-wave2-3-bank-recon chunk B (RATIFICATION-2026-07-13.md) — review_checks/** in surface_scope
from . import bank_recon_check  # noqa: E402,F401 — Wave-2.3 close-walk BankReconCheck (BANK_RECONCILIATION) registers on package import


def get_check(check_id: str) -> Check | None:
    """The Check for an id, or None if unregistered."""
    return CHECK_REGISTRY.get(check_id)


def get_check_for_task_type(task_type: str) -> Check | None:
    """The Check that reviews a close-task task_type, or None if unmapped."""
    check_id = TASK_TYPE_TO_CHECK.get(task_type)
    return CHECK_REGISTRY.get(check_id) if check_id else None


def get_materiality(check_id: str) -> Materiality | None:
    """The registered (Phase-E-overridable) materiality for a check, or None."""
    return CHECK_MATERIALITY.get(check_id)


def _materiality_from_override(override) -> Materiality | None:
    """Reconstruct a ``Materiality`` from a ``ReviewPolicyCheckOverride`` row.

    Maps the override's stored ``shape`` (the MaterialityShape string) to the
    matching named constructor using ``abs_floor`` / ``pct_floor`` (mirrored from
    the Materiality value object). Returns None on an unrecognized shape or a
    floor missing for the shape it requires — the caller then falls back to the
    registry default (NEVER None for a registered check; cond 3). The math is the
    SAME Materiality value object — no new arithmetic.
    """
    shape = (override.shape or "").strip().lower()
    abs_floor = override.abs_floor
    pct_floor = override.pct_floor
    label = override.label or ""

    if shape == MaterialityShape.ABSOLUTE.value:
        if abs_floor is None:
            return None
        return Materiality.absolute(abs_floor, label=label)
    if shape == MaterialityShape.DERIVED.value:
        if abs_floor is None:
            return None
        return Materiality.derived(abs_floor, label=label)
    if shape == MaterialityShape.PERCENT.value:
        if pct_floor is None:
            return None
        return Materiality.percent(pct_floor, label=label)
    if shape == MaterialityShape.DUAL.value:
        if abs_floor is None or pct_floor is None:
            return None
        return Materiality.dual(pct=pct_floor, abs_floor=abs_floor, label=label)
    return None


def get_effective_materiality(db, check_id: str, *, realm_id: str) -> Materiality | None:
    """The tenant's effective materiality for a check: per-company OVERRIDE → registry DEFAULT.

    The Phase-E per-company override resolver (DEC-E-3). Three fallback layers,
    fail-closed at each (cond 3 — NEVER None for a REGISTERED check; the close
    driver's fail-closed branch depends on a registered check always resolving):

      1. The active ``ReviewPolicy`` for ``realm_id`` carries a
         ``ReviewPolicyCheckOverride`` for ``check_id`` → reconstruct the
         per-company ``Materiality`` from it.
      2. No override (no policy / no row / unreconstructable shape) → the
         registry default ``get_materiality(check_id)``.
      3. Any error reading the policy → the registry default (degrade, never
         crash the close walk / nightly run; the per-check try/except in the
         callers is the second wall).

    Read-only on the process-global ``CHECK_MATERIALITY`` — this NEVER mutates the
    worker-shared registry (a per-request mutation would leak one tenant's floor
    into the next request on the same worker). The ReviewPolicy read runs under
    the caller's already-pinned tenant scope (the request/task pins the RLS GUCs);
    it filters by the realm-scoped ``qbo_realm_id`` and does not bypass scope.
    """
    default = get_materiality(check_id)
    try:
        # Local imports: keep package import light (the registries populate on
        # import) and avoid a models import cycle at module load.
        from ...models import ReviewPolicy, ReviewPolicyCheckOverride

        policy = (
            db.query(ReviewPolicy)
            .filter(
                ReviewPolicy.qbo_realm_id == realm_id,
                ReviewPolicy.is_active == True,  # noqa: E712 — SQLAlchemy column truth-test, not a Python bool identity
            )
            .first()
        )
        if policy is None:
            return default

        override = (
            db.query(ReviewPolicyCheckOverride)  # CROSS-TENANT-OK: child-of-realm-scoped-parent — ownership flows transitively via review_policy_id==policy.id whose parent ReviewPolicy was already filtered by the realm-scoped qbo_realm_id above; this child carries no realm triple by design (models.py:3259)
            .filter(
                ReviewPolicyCheckOverride.review_policy_id == policy.id,
                ReviewPolicyCheckOverride.check_id == check_id,
            )
            .first()
        )
        if override is None:
            return default

        reconstructed = _materiality_from_override(override)
        return reconstructed if reconstructed is not None else default
    except Exception:  # noqa: BLE001 — a policy-read failure degrades to the registry default, never crashes the review walk (cond 3)
        return default


def get_tier(check_id: str) -> "CheckTier":
    """The registered static tier for a check; STANDARD if unregistered.

    Defaults to STANDARD (never None) so an unknown/legacy check_id behaves like
    every other STANDARD check under check_runs_under — it runs at balanced.
    """
    return CHECK_TIER.get(check_id, CheckTier.STANDARD)


# The three aggressiveness modes (mirrors ReviewPolicy.review_aggressiveness,
# models.py:3225 default "balanced"). A value outside this set is treated as
# "balanced" by get_effective_aggressiveness + check_runs_under (fail-soft).
_VALID_AGGRESSIVENESS = ("conservative", "balanced", "aggressive")
_DEFAULT_AGGRESSIVENESS = "balanced"


def get_effective_aggressiveness(db, *, realm_id: str) -> str:
    """The tenant's effective review_aggressiveness: per-company POLICY → "balanced".

    MIRRORS ``get_effective_materiality`` (the realm-scoped ReviewPolicy resolver):
    reads the active ``ReviewPolicy`` for ``realm_id`` under the caller's already-
    pinned tenant scope and returns its ``review_aggressiveness``. Fail-soft at
    every layer — NEVER raises (a resolve failure must not crash the nightly run /
    close walk; the per-check try/except in the callers is the second wall):

      1. No policy / no row → "balanced" (the on-disk column default).
      2. A stored value outside {conservative, balanced, aggressive} → "balanced".
      3. Any error reading the policy → "balanced".

    Read-only on a realm-scoped read (filters by the realm-scoped ``qbo_realm_id``;
    does not bypass scope), exactly like the materiality resolver above.
    """
    try:
        from ...models import ReviewPolicy

        policy = (
            db.query(ReviewPolicy)
            .filter(
                ReviewPolicy.qbo_realm_id == realm_id,
                ReviewPolicy.is_active == True,  # noqa: E712 — SQLAlchemy column truth-test, not a Python bool identity
            )
            .first()
        )
        if policy is None:
            return _DEFAULT_AGGRESSIVENESS
        value = (policy.review_aggressiveness or "").strip().lower()
        return value if value in _VALID_AGGRESSIVENESS else _DEFAULT_AGGRESSIVENESS
    except Exception:  # noqa: BLE001 — a policy-read failure degrades to "balanced", never crashes the review walk
        return _DEFAULT_AGGRESSIVENESS


def check_runs_under(aggressiveness: str, tier: "CheckTier") -> bool:
    """Pure: does a check of ``tier`` run under the company's ``aggressiveness``?

    The 3×3 selection matrix (AC-1). An unknown aggressiveness is treated as
    "balanced" (fail-soft — never skip a check on a typo'd policy value):

      - conservative → CRITICAL only.
      - balanced     → CRITICAL + STANDARD (the default; runs every existing
                       check since none ship OPTIONAL → ZERO default regression).
      - aggressive   → CRITICAL + STANDARD + OPTIONAL (all).

    No DB, no I/O — a pure function over (str, CheckTier) so the matrix is
    falsifiable in isolation.
    """
    mode = (aggressiveness or "").strip().lower()
    if mode not in _VALID_AGGRESSIVENESS:
        mode = _DEFAULT_AGGRESSIVENESS
    if mode == "aggressive":
        return True  # all tiers
    if mode == "balanced":
        return tier in (CheckTier.CRITICAL, CheckTier.STANDARD)
    # conservative
    return tier is CheckTier.CRITICAL


__all__ = [
    # Protocol + framework types
    "Check",
    "CheckContext",
    "CheckResult",
    "CheckRunStatus",
    "Finding",
    "FindingOutcome",
    "FindingSeverity",
    "ProposeCallable",
    "run_check",
    # Materiality
    "Materiality",
    "MaterialityShape",
    # Tier / aggressiveness (Phase E check-skip lever)
    "CheckTier",
    "check_runs_under",
    "get_tier",
    "get_effective_aggressiveness",
    # Registries + registration seam
    "CHECK_REGISTRY",
    "TASK_TYPE_TO_CHECK",
    "CHECK_MATERIALITY",
    "CHECK_TIER",
    "register_check",
    "get_check",
    "get_check_for_task_type",
    "get_materiality",
    "get_effective_materiality",
]

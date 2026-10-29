"""
schedule_types.py — the declarative schedule-type descriptor registry.

Plan 20260701-phase-a-generic-schedules, Wave 1 AC-1/AC-2. Every per-type
branch site (engine JE descriptions, je_builder default accounts + entry
types + amortization direction, grid tie-out mode, validate_schedule_balance
BS patterns, detector keyword pass, router type validation + sync config +
import default names, frontend type payload) reads THIS registry instead of
carrying its own literal dict. Adding a schedule type = adding one
descriptor here; no engine/router/frontend branch edits.

Storage note (disk-confirmed): ``AccountingSchedule.schedule_type`` and
``ScheduleCreditMapping.schedule_type`` are plain ``String`` columns — there
is no DB enum — so presets store as their own ``schedule_type`` string and
need no schema change.

Behavioral invariants:
  * The four pre-existing types (PREPAID, ACCRUAL, FIXED_ASSET, INTANGIBLE)
    are byte-identical to the pre-registry literals — locked by
    tests/test_schedule_type_registry_golden.py (written BEFORE the refactor).
  * GENERIC is the open substrate: any BS account can be scheduled.
  * DEFERRED_REVENUE / DEPOSIT / DEBT_PRINCIPAL are PRESETS over GENERIC
    (``preset_of="GENERIC"``): named default accounts / keywords / direction,
    not new engine semantics.

``amortization_direction`` — which side of the JE the P&L/offset account
takes when a POSITIVE schedule-entry amount amortizes:
  * ``"debit_pl"``  — classic expense recognition: DEBIT the P&L account,
    CREDIT the schedule's BS account (prepaid amortization, depreciation,
    accrual build-up, deposit release).
  * ``"credit_pl"`` — recognition INTO the P&L / paydown of a liability:
    DEBIT the schedule's BS account, CREDIT the P&L/offset account
    (deferred revenue → revenue; debt principal → the offset account).
Negative entry amounts (correctives/reversals) flip whichever layout the
direction selects — the sign-aware flip in je_builder / the engine is
direction-relative, not hardcoded to expense.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

# tie_out_mode values
TIE_OUT_REMAINING_VS_BS = "remaining_vs_bs"          # remaining balance vs the BS account
TIE_OUT_CUMULATIVE_VS_CONTRA = "cumulative_vs_contra"  # cumulative amort vs the contra account

# amortization_direction values
DIRECTION_DEBIT_PL = "debit_pl"
DIRECTION_CREDIT_PL = "credit_pl"

# reversal_flavor values (Wave 2 AC-6). "auto_reverse": when an entry of this
# type POSTS, the engine auto-DRAFTs the period+1 opposite-sign reversal entry
# (linked via ScheduleEntry.reversal_of_entry_id) and its JE proposal — the
# reversal is ALWAYS proposal-gated, never auto-posted. Only ACCRUAL carries
# it: an accrued estimate is booked, reversed next period, and replaced by the
# actual. The GENERIC presets deliberately do NOT (deferred revenue / deposits
# / debt principal recognize permanently — reversing them would un-recognize
# real revenue / releases / paydowns).
REVERSAL_AUTO_REVERSE = "auto_reverse"

# AccountingSchedule.automation_policy values (Wave 2 AC-5). Validated at the
# DTO layer; plain String column, no DB enum (matches schedule_type).
AUTOMATION_POLICY_MANUAL = "manual"
AUTOMATION_POLICY_AUTO_PROPOSE = "auto_propose"          # the default
AUTOMATION_POLICY_AUTO_POST = "auto_post_deterministic"
AUTOMATION_POLICIES = (
    AUTOMATION_POLICY_MANUAL,
    AUTOMATION_POLICY_AUTO_PROPOSE,
    AUTOMATION_POLICY_AUTO_POST,
)

#
# AccountingSchedule.depreciation_method values the engine implements. A
# closed set: an unimplemented method (e.g. MACRS) is refused, never computed
# as something else. DECLINING_BALANCE is 200% declining balance with the
# switch to straight-line over the remaining life (see generate_entries).
DEPRECIATION_STRAIGHT_LINE = "STRAIGHT_LINE"
DEPRECIATION_DECLINING_BALANCE = "DECLINING_BALANCE"
DEPRECIATION_METHODS = (DEPRECIATION_STRAIGHT_LINE, DEPRECIATION_DECLINING_BALANCE)
_DEPRECIATION_ALIASES = {
    "DDB": DEPRECIATION_DECLINING_BALANCE,
    "DOUBLE_DECLINING": DEPRECIATION_DECLINING_BALANCE,
    "DOUBLE_DECLINING_BALANCE": DEPRECIATION_DECLINING_BALANCE,
}


def canonical_depreciation_method(value: Optional[str]) -> Optional[str]:
    """Canonical method code for ``value`` (blank → STRAIGHT_LINE; "DDB" and
    spelled-out double-declining forms → DECLINING_BALANCE), or None when the
    engine does not implement it."""
    raw = (value or DEPRECIATION_STRAIGHT_LINE).strip().upper().replace("-", "_").replace(" ", "_")
    method = _DEPRECIATION_ALIASES.get(raw, raw)
    return method if method in DEPRECIATION_METHODS else None


def unsupported_depreciation_method_message(value: Optional[str]) -> str:
    return (
        f"Unsupported depreciation_method {value!r}: supported methods are "
        f"{', '.join(DEPRECIATION_METHODS)} (MACRS is not implemented)."
    )


def normalize_depreciation_method(value: Optional[str]) -> str:
    """canonical_depreciation_method, raising ValueError for anything the
    engine does not implement."""
    method = canonical_depreciation_method(value)
    if method is None:
        raise ValueError(unsupported_depreciation_method_message(value))
    return method


# The engine's fallback when a schedule row somehow has no type.
DEFAULT_SCHEDULE_TYPE = "PREPAID"


@dataclass(frozen=True)
class ScheduleTypeDescriptor:
    """One schedule type, fully described. Every field that used to live in a
    per-file literal dict is named here with the file it was lifted from."""

    code: str
    label: str                                  # frontend badge label
    label_plural: str                           # frontend tab label
    preset_of: Optional[str]                    # None | "GENERIC"
    default_bs_account_name: str                # je_builder _DEFAULT_CREDIT_NAMES
    default_expense_account_name: str           # je_builder _DEFAULT_EXPENSE_NAMES (the P&L/offset side)
    amortization_direction: str                 # DIRECTION_DEBIT_PL | DIRECTION_CREDIT_PL
    tie_out_mode: str                           # TIE_OUT_* (router schedule-grid is_depreciable branch)
    detection_keywords: Tuple[str, ...]         # detector *_KEYWORDS (lower-cased)
    reversal_flavor: Optional[str] = None       # None | REVERSAL_AUTO_REVERSE (Wave 2 AC-6; ACCRUAL only)

    # Branch-site collapse support (each maps 1:1 to a pre-registry literal):
    je_description: str = "Schedule entry"      # schedule_engine JE_DESCRIPTIONS
    memo_template: str = (                      # schedule_engine MEMO_TEMPLATES
        "Auto-generated schedule entry for {name} — {period}"
    )
    entry_type: str = "MANUAL"                  # je_builder _ENTRY_TYPE_MAP (AdjustingEntry.entry_type)
    default_manual_name: str = "Manual"         # router DEFAULT_NAMES
    engine_bs_patterns: Tuple[str, ...] = ()    # schedule_engine BS_ACCOUNT_PATTERNS
    grid_bs_patterns: Tuple[str, ...] = ()      # router schedule-grid _BS_PATTERNS
    tie_out_label: str = "remaining balance"    # router schedule-grid check_label
    balance_label: str = "Total Balance"        # frontend grid footer label
    accum_label: str = "ACCUM. DEPRECIATION"    # frontend accum row label (contra types)
    sync_qbo_account_type: Optional[str] = None  # router SYNC_CONFIG qbo_account_type
    sync_label: Optional[str] = None            # router SYNC_CONFIG label
    sync_endpoint: Optional[str] = None         # frontend SYNC_ENDPOINTS route suffix
    shows_asset_columns: bool = False           # frontend FA grid columns + import-wizard fields

    # Detector rule-pass parameters (detector _rule_based_detect):
    detection_priority: int = 100               # scan order — lower scans first
    detection_confidence: float = 0.0
    detection_default_life_months: Optional[int] = None  # None → keyword-derived or unset
    detection_name_prefix: str = ""             # "Prepaid – {vendor}" etc.
    detection_reason_label: str = ""            # "prepaid" in "matches prepaid keyword '…'"
    infer_asset_class: bool = False             # FA-only keyword→class/life inference

    # Phase-B fixed-asset lifecycle (plan 20260702-phase-b-fa-lifecycle):
    #   salvage_applicable — the type's depreciable base is
    #     original_amount − salvage_value (FA + INTANGIBLE). Types where this
    #     is False must carry salvage_value == 0 (enforced at the DTO layer),
    #     so the engine can subtract salvage unconditionally and every
    #     pre-Phase-B schedule (salvage 0) behaves byte-identically.
    #   supports_disposal — POST /accounting/schedules/{id}/dispose drafts the
    #     proposal-gated gain/loss disposal JE for this type.
    #   default_gain_loss_account_name — the P&L account the disposal JE plugs
    #     gain/loss to when the caller does not pass an explicit account
    #     (resolved by name against the local QBOAccount cache).
    salvage_applicable: bool = False
    supports_disposal: bool = False
    default_gain_loss_account_name: str = ""


# Ordered: the first four preserve the legacy frontend tab order
# (PREPAID, FIXED_ASSET, INTANGIBLE, ACCRUAL), then the generic substrate and
# its presets. Detection scan order is detection_priority, NOT this order.
SCHEDULE_TYPES: dict[str, ScheduleTypeDescriptor] = {
    d.code: d
    for d in (
        ScheduleTypeDescriptor(
            code="PREPAID",
            label="Prepaid",
            label_plural="Prepaids",
            preset_of=None,
            default_bs_account_name="Prepaid Expenses",
            default_expense_account_name="Prepaid Expenses Amortization",
            amortization_direction=DIRECTION_DEBIT_PL,
            tie_out_mode=TIE_OUT_REMAINING_VS_BS,
            detection_keywords=(
                "insurance", "prepaid", "premium", "annual fee", "annual subscription",
                "yearly subscription", "maintenance agreement", "service agreement",
                "support contract", "license renewal", "warranty",
            ),
            je_description="Prepaid amortization",
            memo_template="Auto-generated prepaid amortization for {name} — {period}",
            entry_type="PREPAID_AMORTIZATION",
            default_manual_name="Manual Prepaid",
            engine_bs_patterns=("prepaid", "prepaid expenses", "prepaid expense"),
            grid_bs_patterns=("prepaid",),
            tie_out_label="remaining balance",
            balance_label="Total Balance",
            sync_endpoint="sync-prepaids",  # AI-assisted sync, not SYNC_CONFIG-driven
            detection_priority=30,
            detection_confidence=0.70,
            detection_default_life_months=12,
            detection_name_prefix="Prepaid",
            detection_reason_label="prepaid",
        ),
        ScheduleTypeDescriptor(
            code="FIXED_ASSET",
            label="Fixed Asset",
            label_plural="Fixed Assets",
            preset_of=None,
            default_bs_account_name="Accumulated Depreciation",
            default_expense_account_name="Depreciation Expense",
            amortization_direction=DIRECTION_DEBIT_PL,
            tie_out_mode=TIE_OUT_CUMULATIVE_VS_CONTRA,
            detection_keywords=(
                "equipment", "machinery", "computer", "laptop", "server", "vehicle",
                "truck", "automobile", "furniture", "fixture", "leasehold improvement",
                "build-out", "buildout", "construction", "renovation", "forklift",
                "printer", "monitor", "workstation",
            ),
            je_description="Depreciation expense",
            memo_template="Auto-generated depreciation for {name} — {period}",
            entry_type="DEPRECIATION",
            default_manual_name="Manual Fixed Asset",
            engine_bs_patterns=("property", "equipment", "fixed asset", "furniture", "leasehold"),
            grid_bs_patterns=(
                "accumulated depreciation", "accum depreciation",
                "accum. depreciation", "accum depr",
            ),
            tie_out_label="accumulated depreciation",
            balance_label="Net Book Value",
            accum_label="ACCUM. DEPRECIATION",
            sync_qbo_account_type="Fixed Asset",
            sync_label="fixed asset",
            sync_endpoint="sync-fixed-assets",
            shows_asset_columns=True,
            detection_priority=10,
            detection_confidence=0.65,
            detection_default_life_months=None,  # keyword-derived (infer_asset_class)
            detection_name_prefix="Fixed Asset",
            detection_reason_label="fixed-asset",
            infer_asset_class=True,
            salvage_applicable=True,
            supports_disposal=True,
            default_gain_loss_account_name="Gain/Loss on Disposal of Assets",
        ),
        ScheduleTypeDescriptor(
            code="INTANGIBLE",
            label="Intangible",
            label_plural="Intangibles",
            preset_of=None,
            default_bs_account_name="Accumulated Amortization",
            default_expense_account_name="Amortization Expense",
            amortization_direction=DIRECTION_DEBIT_PL,
            tie_out_mode=TIE_OUT_CUMULATIVE_VS_CONTRA,
            detection_keywords=(
                "software license", "patent", "trademark", "copyright", "franchise",
                "goodwill", "customer list", "non-compete", "domain", "brand",
                "perpetual license", "software acquisition",
            ),
            je_description="Intangible amortization",
            memo_template="Auto-generated intangible amortization for {name} — {period}",
            entry_type="AMORTIZATION",
            default_manual_name="Manual Intangible",
            engine_bs_patterns=("intangible", "goodwill", "software", "patent"),
            grid_bs_patterns=(
                "accumulated amortization", "accum amortization",
                "accum. amortization", "accum amort",
            ),
            tie_out_label="accumulated amortization",
            balance_label="Total Balance",
            accum_label="ACCUM. AMORTIZATION",
            sync_qbo_account_type="Other Asset",
            sync_label="intangible",
            sync_endpoint="sync-intangibles",
            detection_priority=20,
            detection_confidence=0.65,
            detection_default_life_months=60,
            detection_name_prefix="Intangible",
            detection_reason_label="intangible",
            salvage_applicable=True,
            supports_disposal=True,
            default_gain_loss_account_name="Gain/Loss on Disposal of Assets",
        ),
        ScheduleTypeDescriptor(
            code="ACCRUAL",
            label="Accrual",
            label_plural="Accruals",
            preset_of=None,
            default_bs_account_name="Accrued Liabilities",
            default_expense_account_name="Accrued Expenses",
            amortization_direction=DIRECTION_DEBIT_PL,
            tie_out_mode=TIE_OUT_REMAINING_VS_BS,
            detection_keywords=("accrued", "accrual", "estimated", "unbilled", "provision"),
            reversal_flavor=REVERSAL_AUTO_REVERSE,  # AC-6: posted accrual → auto-draft the period+1 reversal (proposal-gated)
            je_description="Accrual expense recognition",
            memo_template="Auto-generated accrual recognition for {name} — {period}",
            entry_type="ACCRUAL",
            default_manual_name="Manual Accrual",
            engine_bs_patterns=("accrued", "accrued expenses", "accrued liabilities"),
            grid_bs_patterns=("accrued",),
            tie_out_label="remaining balance",
            balance_label="Total Balance",
            sync_qbo_account_type="Other Current Liability",
            sync_label="accrual",
            sync_endpoint="sync-accruals",
            detection_priority=40,
            detection_confidence=0.60,
            detection_default_life_months=None,
            detection_name_prefix="Accrual",
            detection_reason_label="accrual",
        ),
        # ── AC-2: the generic substrate + presets ────────────────────────────
        ScheduleTypeDescriptor(
            code="GENERIC",
            label="Generic",
            label_plural="Generic Schedules",
            preset_of=None,
            default_bs_account_name="",
            default_expense_account_name="",
            amortization_direction=DIRECTION_DEBIT_PL,
            tie_out_mode=TIE_OUT_REMAINING_VS_BS,
            detection_keywords=(),  # never keyword-detected; user-created only
            je_description="Schedule amortization",
            memo_template="Auto-generated schedule entry for {name} — {period}",
            entry_type="AMORTIZATION",
            default_manual_name="Manual Schedule",
            engine_bs_patterns=(),
            grid_bs_patterns=(),
            tie_out_label="remaining balance",
            balance_label="Total Balance",
            detection_priority=99,
            detection_confidence=0.0,
            detection_name_prefix="Schedule",
            detection_reason_label="generic",
        ),
        ScheduleTypeDescriptor(
            code="DEFERRED_REVENUE",
            label="Deferred Revenue",
            label_plural="Deferred Revenue",
            preset_of="GENERIC",
            default_bs_account_name="Deferred Revenue",
            default_expense_account_name="Revenue",  # the P&L side is CREDITED (credit_pl)
            amortization_direction=DIRECTION_CREDIT_PL,
            tie_out_mode=TIE_OUT_REMAINING_VS_BS,
            detection_keywords=(
                "deferred revenue", "unearned revenue", "customer prepayment",
                "advance billing", "annual contract billed",
            ),
            je_description="Deferred revenue recognition",
            memo_template="Auto-generated revenue recognition for {name} — {period}",
            entry_type="REVENUE_RECOGNITION",
            default_manual_name="Manual Deferred Revenue",
            engine_bs_patterns=("deferred revenue", "unearned revenue"),
            grid_bs_patterns=("deferred revenue", "unearned revenue", "deferred rev"),
            tie_out_label="remaining balance",
            balance_label="Total Balance",
            detection_priority=50,
            detection_confidence=0.60,
            detection_default_life_months=12,
            detection_name_prefix="Deferred Revenue",
            detection_reason_label="deferred-revenue",
        ),
        ScheduleTypeDescriptor(
            code="DEPOSIT",
            label="Deposit",
            label_plural="Deposits",
            preset_of="GENERIC",
            default_bs_account_name="Security Deposits",
            default_expense_account_name="",  # released to whatever P&L account the user maps
            amortization_direction=DIRECTION_DEBIT_PL,
            tie_out_mode=TIE_OUT_REMAINING_VS_BS,
            detection_keywords=("security deposit", "deposit paid", "retainer", "escrow deposit"),
            je_description="Deposit release",
            memo_template="Auto-generated deposit release for {name} — {period}",
            entry_type="DEPOSIT_RELEASE",
            default_manual_name="Manual Deposit",
            engine_bs_patterns=("deposit",),
            grid_bs_patterns=("deposit",),
            tie_out_label="remaining balance",
            balance_label="Total Balance",
            detection_priority=60,
            detection_confidence=0.55,
            detection_default_life_months=12,
            detection_name_prefix="Deposit",
            detection_reason_label="deposit",
        ),
        ScheduleTypeDescriptor(
            code="DEBT_PRINCIPAL",
            label="Debt Principal",
            label_plural="Debt Principal",
            preset_of="GENERIC",
            default_bs_account_name="Notes Payable",
            default_expense_account_name="",  # the offset account the user maps (clearing/cash-tracking)
            # A positive entry DEBITS the liability (principal paydown) and
            # CREDITS the mapped offset account — the credit_pl layout.
            amortization_direction=DIRECTION_CREDIT_PL,
            tie_out_mode=TIE_OUT_REMAINING_VS_BS,
            detection_keywords=("loan principal", "note payable", "term loan", "principal payment"),
            je_description="Debt principal reduction",
            memo_template="Auto-generated principal reduction for {name} — {period}",
            entry_type="DEBT_PRINCIPAL",
            default_manual_name="Manual Debt Schedule",
            engine_bs_patterns=("note payable", "notes payable", "loan payable"),
            grid_bs_patterns=("note payable", "notes payable", "loan payable"),
            tie_out_label="remaining balance",
            balance_label="Total Balance",
            detection_priority=70,
            detection_confidence=0.55,
            detection_default_life_months=None,
            detection_name_prefix="Debt",
            detection_reason_label="debt-principal",
        ),
    )
}


def get_descriptor(code: Optional[str]) -> ScheduleTypeDescriptor:
    """Descriptor for ``code``; unknown/empty codes fall back to the engine's
    historical default (PREPAID) — mirrors the pre-registry
    ``schedule.schedule_type or "PREPAID"`` fallbacks."""
    return SCHEDULE_TYPES.get(code or DEFAULT_SCHEDULE_TYPE, SCHEDULE_TYPES[DEFAULT_SCHEDULE_TYPE])


def valid_type_codes() -> set[str]:
    """The set the routers validate ``schedule_type`` query params against."""
    return set(SCHEDULE_TYPES.keys())


def is_valid_type(code: str) -> bool:
    return code in SCHEDULE_TYPES


def detection_ordered_descriptors() -> list[ScheduleTypeDescriptor]:
    """Descriptors with detection keywords, in scan order (lowest
    detection_priority first). Preserves the legacy FA → INTANGIBLE →
    PREPAID → ACCRUAL keyword precedence."""
    return sorted(
        (d for d in SCHEDULE_TYPES.values() if d.detection_keywords),
        key=lambda d: d.detection_priority,
    )


def types_payload() -> list[dict]:
    """The frontend contract: everything the schedules UI needs to render a
    type without a hardcoded list (served by GET /accounting/schedule-types)."""
    return [
        {
            "code": d.code,
            "label": d.label,
            "label_plural": d.label_plural,
            "preset_of": d.preset_of,
            "amortization_direction": d.amortization_direction,
            "tie_out_mode": d.tie_out_mode,
            "balance_label": d.balance_label,
            "accum_label": d.accum_label,
            "shows_gross_accum": d.tie_out_mode == TIE_OUT_CUMULATIVE_VS_CONTRA,
            "shows_asset_columns": d.shows_asset_columns,
            "sync_endpoint": d.sync_endpoint,
            "default_bs_account_name": d.default_bs_account_name,
            "default_expense_account_name": d.default_expense_account_name,
            "salvage_applicable": d.salvage_applicable,
            "supports_disposal": d.supports_disposal,
        }
        for d in SCHEDULE_TYPES.values()
    ]

"""Subledger-to-GL reconciliation-break detector — Phase C NEW check (AC-3/AC-4).

Ties a control account's SUBLEDGER total to its GL/balance-sheet balance and
flags a break (they don't tie out). The classic close-review control: the AR
subledger (sum of open customer balances) must equal the GL Accounts-Receivable
control-account balance; likewise AP vs the Accounts-Payable control. A
persistent gap means a direct GL JE bypassed the subledger (or a sync gap).

DATA SOURCES (two independent reads that MUST agree):
  - SUBLEDGER total: the aging report total — ``ar_aging(...)['total_ar']``
    (app/analysis/ar_aging.py:11,147) / ``ap_aging(...)['total_ap']``
    (app/analysis/ap_aging.py:25,154). This is the customer/vendor-level
    open-balance roll-up (the subledger).
  - GL control balance: the Balance-Sheet control-account total via the SAME
    BalanceSheet report + ``_get_bs_total`` walk the covenant builtins reuse
    (app/services/coa_formula.py:328-341 _compute_current_ratio,
    :375 _get_bs_total). We read "Accounts Receivable" / "Accounts Payable".

FAIL-CLOSED HONEST DEGRADATION (DEC-CAR-C-4 — the load-bearing rule for THIS
check): if EITHER side is missing/unreadable, or the GL control line is absent
from the Balance Sheet, the detector returns ``available=False`` →
``Finding.could_not_verify``. It NEVER substitutes ``or 0`` / ``Decimal("0")``
for a missing side and reports a (fabricated) clean tie-out — a recon that cannot
see one side has NOT proven the books tie.
"""

from __future__ import annotations

import logging
from datetime import date

from ..qbo_client import QBOClient
from ..services.coa_formula import _get_bs_total
from .ar_aging import ar_aging
from .ap_aging import ap_aging

logger = logging.getLogger(__name__)

# Absolute tie-out tolerance (a sub-dollar GL rounding gap is not a break).
RECON_TOLERANCE = 1.0

# The Balance-Sheet control-account row labels QBO emits for each side.
_BS_LABELS = {
    "AR": ["Accounts Receivable", "Accounts Receivable (A/R)", "Total Accounts Receivable"],
    "AP": ["Accounts Payable", "Accounts Payable (A/P)", "Total Accounts Payable"],
}


def _subledger_total(qbo_client: QBOClient, side: str, as_of: date) -> float | None:
    """The aging-report subledger total for AR/AP, or None on a degraded source."""
    try:
        if side == "AP":
            return ap_aging(qbo_client, as_of_date=as_of).get("total_ap")
        return ar_aging(qbo_client, as_of_date=as_of).get("total_ar")
    except Exception as e:  # ar_aging/ap_aging RAISE on unreadable report
        logger.warning("[subledger_recon] %s subledger unavailable: %s", side, type(e).__name__)
        return None


def _gl_control_balance(qbo_client: QBOClient, side: str, as_of: date) -> float | None:
    """The GL control-account balance from the Balance Sheet, or None if absent.

    None (not 0) when the BS read fails OR the control row is absent — a missing
    control line is 'unknown', never a fabricated zero balance (DEC-CAR-C-4).
    """
    try:
        bs = qbo_client.get_report("BalanceSheet", {
            "start_date": f"{as_of.year}-01-01",
            "end_date": as_of.isoformat(),
            "accounting_method": "Accrual",
        })
    except Exception as e:
        logger.warning("[subledger_recon] %s BalanceSheet unavailable: %s", side, type(e).__name__)
        return None
    return _get_bs_total(bs, _BS_LABELS[side])  # returns None if the control row is absent


def subledger_to_gl_recon(
    qbo_client: QBOClient,
    side: str = "AR",
    as_of_date: "date | None" = None,
) -> dict:
    """Reconcile the AR/AP subledger total to its GL control balance.

    Returns a dict carrying ``available`` (False ⇒ could_not_verify), the two
    sides, the absolute break, and ``tied_out``. FAIL-CLOSED: any missing side →
    available=False (never a fabricated clean tie-out).
    """
    s = side.upper()
    if s not in _BS_LABELS:
        s = "AR"
    today = as_of_date or date.today()

    subledger = _subledger_total(qbo_client, s, today)
    gl_control = _gl_control_balance(qbo_client, s, today)

    # FAIL-CLOSED: a recon needs BOTH sides. A missing side is could-not-verify,
    # NOT a substituted 0 that fabricates a clean (or fabricated-broken) tie-out.
    if subledger is None or gl_control is None:
        missing = "subledger total" if subledger is None else "GL control balance"
        return {
            "available": False,
            "side": s,
            "reason": f"{s} {missing} could not be loaded — cannot tie out",
        }

    break_amount = round(abs(subledger - gl_control), 2)
    tied_out = break_amount <= RECON_TOLERANCE
    return {
        "available": True,
        "side": s,
        "as_of": today.isoformat(),
        "subledger_total": round(subledger, 2),
        "gl_control_balance": round(gl_control, 2),
        "break_amount": break_amount,
        "tied_out": tied_out,
    }

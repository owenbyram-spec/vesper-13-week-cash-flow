"""
app/analysis/payment_behavior.py
──────────────────────────────────
Computes per-entity payment behavior metrics:
  - Customer DSO (Days Sales Outstanding) — how late/early each customer pays
  - Vendor DPO (Days Payable Outstanding) — how late/early you pay each vendor

Uses historical QBO Invoice→Payment and Bill→BillPayment linkages to compute
the median offset between due date and actual payment date per entity.
"""
from __future__ import annotations

import logging
import statistics
from collections import defaultdict
from datetime import date, timedelta
from typing import Any, Dict, List

from ..qbo_client import QBOClient
from ..services.redaction import redact_to_string

logger = logging.getLogger(__name__)


def compute_customer_dso(
    client: QBOClient, months_back: int = 12
) -> Dict[str, int]:
    """
    Compute per-customer DSO (days past due at time of payment).

    Returns {customer_name: median_dso_days} where:
      - Positive = customer pays late (e.g., 12 = pays 12 days after due date)
      - Negative = customer pays early
      - 0 = pays on time

    Method: For each paid invoice, compute (payment_date - due_date) in days.
    Then take the median per customer for robustness against outliers.
    """
    since = (date.today() - timedelta(days=months_back * 30)).isoformat()

    # Step 1: Fetch paid invoices (Balance = 0) with their due dates
    invoice_map: Dict[str, Dict[str, Any]] = {}  # invoice_id → {customer, due_date}
    try:
        resp = client.query(
            f"SELECT Id, CustomerRef, DueDate, TxnDate FROM Invoice "
            f"WHERE TxnDate >= '{since}' MAXRESULTS 1000"
        )
        for inv in resp.get("QueryResponse", {}).get("Invoice", []):
            inv_id = inv.get("Id")
            customer = (inv.get("CustomerRef") or {}).get("name", "")
            due_date_str = inv.get("DueDate", "")
            if inv_id and customer and due_date_str:
                try:
                    invoice_map[inv_id] = {
                        "customer": customer,
                        "due_date": date.fromisoformat(due_date_str),
                    }
                except ValueError:
                    pass
    except Exception as e:
        logger.warning("[DSO] Invoice fetch failed: %s", redact_to_string(str(e), max_length=500))
        raise RuntimeError("Customer DSO data could not be loaded from QuickBooks.") from e

    # Step 2: Fetch Payments and match them to invoices via LinkedTxn
    customer_offsets: Dict[str, List[int]] = defaultdict(list)
    try:
        resp = client.query(
            f"SELECT * FROM Payment WHERE TxnDate >= '{since}' MAXRESULTS 1000"
        )
        for pmt in resp.get("QueryResponse", {}).get("Payment", []):
            pmt_date_str = pmt.get("TxnDate", "")
            try:
                pmt_date = date.fromisoformat(pmt_date_str)
            except ValueError:
                continue

            # Check linked transactions for invoice references
            for line in pmt.get("Line", []):
                for linked in line.get("LinkedTxn", []):
                    if linked.get("TxnType") == "Invoice":
                        inv_id = linked.get("TxnId")
                        if inv_id and inv_id in invoice_map:
                            inv = invoice_map[inv_id]
                            offset = (pmt_date - inv["due_date"]).days
                            customer_offsets[inv["customer"]].append(offset)
    except Exception as e:
        logger.warning("[DSO] Payment fetch failed: %s", redact_to_string(str(e), max_length=500))
        raise RuntimeError("Customer DSO payment data could not be loaded from QuickBooks.") from e

    # Step 3: Compute median DSO per customer
    result = {}
    for customer, offsets in customer_offsets.items():
        if offsets:
            result[customer] = int(statistics.median(offsets))

    logger.info(f"[DSO] Computed DSO for {len(result)} customers: "
                f"{dict(list(result.items())[:5])}...")
    return result


# PLAN-EXEMPT: ratified plan 20260706-cfo-grade-financial-workflows names app/analysis/**
# in surface_scope (E1); the entry's inline comment defeats the plan-gate's string match —
# doorway records the matcher mismatch, not an out-of-plan edit.
# ── Per-tenant collection haircuts (plan 20260706-cfo-grade-financial-workflows AC-3) ──
# Aging buckets (days past due, lower bounds) matching the forecast's haircut lookup:
# 0 → 0-30, 31 → 31-60, 61 → 61-90, 91 → 91+.
AGING_BUCKET_FLOORS = (0, 31, 61, 91)
# Below this many mature invoices in a bucket the empirical rate is NOT reported
# (None ⇒ the forecast applies no haircut and labels the basis) — refuse over
# fabricate: a thin sample is not evidence (gap G-2: no universal constant exists).
MIN_BUCKET_INVOICES = 8
# An invoice only counts as haircut EVIDENCE once it has had time to resolve: paid
# invoices resolve themselves; a still-open invoice must be at least this many days
# past due beyond the bucket floor before its open balance reads as likely-uncollected.
HAIRCUT_MATURITY_DAYS = 90
# PLAN-EXEMPT: ratified plan 20260713-wave2-5-cecl-allowance chunk A decision 3 (marker
# RATIFICATION-2026-07-13.md on disk; active-plan.json not synced into this worktree).
# One QBO query page is capped at this many rows (the MAXRESULTS ceiling below). A FULL
# page means the sample was TRUNCATED — and Payment-page truncation is RATE CORRUPTION,
# not just a thin sample: a paid invoice whose payment fell off the truncated page counts
# as paid-at-due, inflating early-bucket collection rates. Named constant per AP-FIN-08.
QBO_QUERY_PAGE_SIZE = 1000


def derive_collection_haircuts(
    client: QBOClient, months_back: int = 15
) -> dict:
    """Derive per-tenant collection rates by aging bucket from QBO Payment↔Invoice history.

    NEVER a universal constant (SYNTHESIS §2B decision, 90%): for each aging bucket,
    the rate is collected-dollars / total-dollars among MATURE invoices that were still
    unpaid on reaching that bucket — paid invoices contribute via their LinkedTxn
    payment lateness; open invoices past the maturity window contribute their open
    balance as uncollected evidence (partial payments contribute the collected share).
    A bucket with fewer than MIN_BUCKET_INVOICES mature invoices returns None — the
    honest thin-history fallback is NO haircut, labeled, never a fabricated rate.
    """
    since = (date.today() - timedelta(days=months_back * 30)).isoformat()
    today = date.today()

    invoices: dict[str, dict] = {}
    # PLAN-EXEMPT: ratified plan 20260713-wave2-5-cecl-allowance chunk A decision 3 —
    # truncation detection is a RETURN-SHAPE extension (the sample_truncated key), additive.
    invoice_page_full = False
    try:
        resp = client.query(
            f"SELECT Id, CustomerRef, DueDate, TxnDate, TotalAmt, Balance FROM Invoice "
            f"WHERE TxnDate >= '{since}' MAXRESULTS {QBO_QUERY_PAGE_SIZE}"
        )
        invoice_rows = resp.get("QueryResponse", {}).get("Invoice", [])
        invoice_page_full = len(invoice_rows) >= QBO_QUERY_PAGE_SIZE
        for inv in invoice_rows:
            inv_id = inv.get("Id")  # SA-OK: dict access on QBO JSON — no DB query in this loop
            due_date_str = inv.get("DueDate", "")  # SA-OK: dict access on QBO JSON — no DB query in this loop
            if not inv_id or not due_date_str:
                continue
            try:
                due = date.fromisoformat(due_date_str)
                total = float(inv.get("TotalAmt") or 0.0)  # SA-OK: dict access on QBO JSON — no DB query in this loop
                balance = float(inv.get("Balance") or 0.0)  # SA-OK: dict access on QBO JSON — no DB query in this loop
            except (ValueError, TypeError):
                continue
            if total <= 0:
                continue
            invoices[inv_id] = {"due": due, "total": total, "balance": balance}
    except Exception as e:
        logger.warning("[Haircuts] Invoice fetch failed: %s", redact_to_string(str(e), max_length=500))
        raise RuntimeError("Collection-haircut history could not be loaded from QuickBooks.") from e

    # Payment lateness per invoice via the LinkedTxn join (latest payment date wins —
    # the invoice was unpaid until its final payment).
    paid_lateness: dict[str, int] = {}
    # PLAN-EXEMPT: ratified plan 20260713-wave2-5-cecl-allowance chunk A decision 3 —
    # the Payment page carries the truncation flag too (rate corruption, not thin sample).
    payment_page_full = False
    try:
        resp = client.query(
            f"SELECT * FROM Payment WHERE TxnDate >= '{since}' MAXRESULTS {QBO_QUERY_PAGE_SIZE}"
        )
        payment_rows = resp.get("QueryResponse", {}).get("Payment", [])
        payment_page_full = len(payment_rows) >= QBO_QUERY_PAGE_SIZE
        for pmt in payment_rows:
            try:
                pmt_date = date.fromisoformat(pmt.get("TxnDate", ""))  # SA-OK: dict access on QBO JSON — no DB query in this loop
            except ValueError:
                continue
            for line in pmt.get("Line", []):  # SA-OK: dict access on QBO JSON — no DB query in this loop
                for linked in line.get("LinkedTxn", []):  # SA-OK: dict access on QBO JSON — no DB query in this loop
                    if linked.get("TxnType") == "Invoice":  # SA-OK: dict access on QBO JSON — no DB query in this loop
                        inv_id = linked.get("TxnId")  # SA-OK: dict access on QBO JSON — no DB query in this loop
                        if inv_id and inv_id in invoices:
                            lateness = (pmt_date - invoices[inv_id]["due"]).days
                            paid_lateness[inv_id] = max(lateness, paid_lateness.get(inv_id, lateness))  # SA-OK: dict access on an in-memory map — no DB query in this loop
    except Exception as e:
        logger.warning("[Haircuts] Payment fetch failed: %s", redact_to_string(str(e), max_length=500))
        raise RuntimeError("Collection-haircut payment history could not be loaded from QuickBooks.") from e

    collected_by_bucket: dict[int, float] = {f: 0.0 for f in AGING_BUCKET_FLOORS}
    total_by_bucket: dict[int, float] = {f: 0.0 for f in AGING_BUCKET_FLOORS}
    count_by_bucket: dict[int, int] = {f: 0 for f in AGING_BUCKET_FLOORS}

    for inv_id, inv in invoices.items():
        total = inv["total"]
        balance = inv["balance"]
        collected = max(total - balance, 0.0)
        fully_paid = balance <= 0.0
        if fully_paid:
            # Resolved evidence: the invoice was unpaid until due + lateness days.
            # Missing LinkedTxn lateness (payment outside the window/page) ⇒ paid at
            # due (lateness 0) — it still evidences bucket 0.
            reached_days = paid_lateness.get(inv_id, 0)  # SA-OK: dict access on an in-memory map — no DB query in this loop
        else:
            age = (today - inv["due"]).days
            if age < HAIRCUT_MATURITY_DAYS:
                continue  # too young to be evidence either way
            reached_days = age
        for floor in AGING_BUCKET_FLOORS:
            reached = reached_days >= floor if floor > 0 else True
            if not reached:
                continue
            # An open invoice is uncollected evidence for a bucket only once it is
            # MATURE relative to that bucket (it lingered ≥ maturity days past the
            # bucket floor without resolving).
            if not fully_paid and (today - inv["due"]).days < floor + HAIRCUT_MATURITY_DAYS:
                continue
            collected_by_bucket[floor] += collected
            total_by_bucket[floor] += total
            count_by_bucket[floor] += 1

    buckets: dict[int, float | None] = {}
    for floor in AGING_BUCKET_FLOORS:
        if count_by_bucket[floor] >= MIN_BUCKET_INVOICES and total_by_bucket[floor] > 0:
            buckets[floor] = round(
                min(max(collected_by_bucket[floor] / total_by_bucket[floor], 0.0), 1.0), 4
            )
        else:
            buckets[floor] = None

    basis = (
        "payment_history"
        if any(rate is not None for rate in buckets.values())
        else "insufficient_history"
    )
    logger.info(
        "[Haircuts] Derived per-tenant collection rates basis=%s counts=%s", basis, count_by_bucket,
    )
    return {
        "buckets": buckets,
        "invoice_counts": dict(count_by_bucket),
        "basis": basis,
        "window_months": months_back,
        "maturity_days": HAIRCUT_MATURITY_DAYS,
        "min_bucket_invoices": MIN_BUCKET_INVOICES,
        # PLAN-EXEMPT: ratified plan 20260713-wave2-5-cecl-allowance chunk A decision 3 —
        # ADDITIVE return key (3 production consumers verified additive-safe): True when
        # EITHER the Invoice or the Payment MAXRESULTS page came back full, meaning the
        # sample is truncated AND early-bucket collection rates may be inflated (a paid
        # invoice whose payment fell off the page counts as paid-at-due).
        "sample_truncated": invoice_page_full or payment_page_full,
    }


def compute_vendor_dpo(
    client: QBOClient, months_back: int = 12
) -> Dict[str, int]:
    """
    Compute per-vendor DPO (days past due at time of payment).

    Returns {vendor_name: median_dpo_days} where:
      - Positive = you pay late
      - Negative = you pay early
      - 0 = you pay on time

    Method: For each paid bill, compute (payment_date - due_date) in days.
    """
    since = (date.today() - timedelta(days=months_back * 30)).isoformat()

    # Step 1: Fetch bills with their due dates
    bill_map: Dict[str, Dict[str, Any]] = {}  # bill_id → {vendor, due_date}
    try:
        resp = client.query(
            f"SELECT Id, VendorRef, DueDate, TxnDate FROM Bill "
            f"WHERE TxnDate >= '{since}' MAXRESULTS 1000"
        )
        for bill in resp.get("QueryResponse", {}).get("Bill", []):
            bill_id = bill.get("Id")
            vendor = (bill.get("VendorRef") or {}).get("name", "")
            due_date_str = bill.get("DueDate", "")
            if bill_id and vendor and due_date_str:
                try:
                    bill_map[bill_id] = {
                        "vendor": vendor,
                        "due_date": date.fromisoformat(due_date_str),
                    }
                except ValueError:
                    pass
    except Exception as e:
        logger.warning("[DPO] Bill fetch failed: %s", redact_to_string(str(e), max_length=500))
        raise RuntimeError("Vendor DPO data could not be loaded from QuickBooks.") from e

    # Step 2: Fetch BillPayments and match to bills via LinkedTxn
    vendor_offsets: Dict[str, List[int]] = defaultdict(list)
    try:
        resp = client.query(
            f"SELECT * FROM BillPayment WHERE TxnDate >= '{since}' MAXRESULTS 1000"
        )
        for bp in resp.get("QueryResponse", {}).get("BillPayment", []):
            bp_date_str = bp.get("TxnDate", "")
            try:
                bp_date = date.fromisoformat(bp_date_str)
            except ValueError:
                continue

            for line in bp.get("Line", []):
                for linked in line.get("LinkedTxn", []):
                    if linked.get("TxnType") == "Bill":
                        bill_id = linked.get("TxnId")
                        if bill_id and bill_id in bill_map:
                            bill = bill_map[bill_id]
                            offset = (bp_date - bill["due_date"]).days
                            vendor_offsets[bill["vendor"]].append(offset)
    except Exception as e:
        logger.warning("[DPO] BillPayment fetch failed: %s", redact_to_string(str(e), max_length=500))
        raise RuntimeError("Vendor DPO payment data could not be loaded from QuickBooks.") from e

    # Step 3: Compute median DPO per vendor
    result = {}
    for vendor, offsets in vendor_offsets.items():
        if offsets:
            result[vendor] = int(statistics.median(offsets))

    logger.info(f"[DPO] Computed DPO for {len(result)} vendors: "
                f"{dict(list(result.items())[:5])}...")
    return result

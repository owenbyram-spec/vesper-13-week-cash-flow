"""Unusual-journal-entry-structure detector — Phase C NEW check (AC-3/AC-4).

A pure ``f(qbo_client, ...) -> dict`` in the established app/analysis shape
(anomalies.py / ar_aging.py): it pulls the manual journal-entry population from
QBO and flags entries whose STRUCTURE is unusual — round-number amounts, weekend
postings, backdated entries, an out-of-balance entry, a suspense-account hit, and
duplicate entries. It is ZERO-LLM and degrades honestly (no-data sentinel) so the
Check wrapper (new_checks.py) can emit ``Finding.could_not_verify`` rather than a
fabricated clean pass (DEC-CAR-C-4).

READ-ONLY: this detector ONLY READS journal entries via a SELECT query; it never
POSTs / updates / voids a journal entry, so the JE-write balance+idempotency
convention (AP-QBO-06) does not apply here (no GL write occurs anywhere below).

DATA SOURCE: ``qbo_client.query("SELECT * FROM JournalEntry ...")``
(app/qbo_client.py:169 ``QBOClient.query``). Each entry carries a ``Line``
array; each line's ``JournalEntryLineDetail`` carries ``PostingType``
("Debit"/"Credit") and ``AccountRef`` (app/routers/accounting.py:52,109 confirm
the QBO line shape), plus an ``Amount`` per line, ``TxnDate``, ``PrivateNote``,
``DocNumber``.

AMOUNT-ANOMALY METHOD (DEC-CAR-C-7 / AP-FIN-06, financial-modeling-correctness
SKILL principle 7): the amount-outlier signal uses a TWO-SIDED modified z-score
built on the MEDIAN and MAD (median absolute deviation), threshold 3.5 — NOT the
one-sided ``mean + pstdev`` of the legacy anomalies.py:46-54. ``mean+pstdev`` lets
the very outliers being hunted inflate sigma and mask themselves (heavy-tailed
amounts), and a one-sided ``z >= t`` misses the negative tail. Both tails are
fraud-relevant, so we flag ``abs(modified_z) >= 3.5``.
"""

from __future__ import annotations

import logging
import statistics
from datetime import datetime

from ..qbo_client import QBOClient

logger = logging.getLogger(__name__)

# ── named, auditable thresholds (financial-modeling.md AP-FIN-08: name every
#    finance coefficient rather than burying it in the math) ───────────────────
MODIFIED_Z_THRESHOLD = 3.5  # robust modified-z cutoff (median+MAD); Iglewicz-Hoaglin standard
# 0.6745 = the 0.75 quantile of the standard normal; it scales MAD so the modified
# z-score is comparable to a classic z under normality (Iglewicz & Hoaglin 1993).
MAD_NORMAL_SCALE = 0.6745
ROUND_NUMBER_MODULO = 1000.0  # an amount that is an exact multiple of this is "round"
BALANCE_TOLERANCE = 0.01  # debits-vs-credits tie-out tolerance (one cent)
# Substrings that mark a clearing/suspense account by name — an entry touching one
# of these is a structural signal (suspense balances should clear, not persist).
SUSPENSE_ACCOUNT_MARKERS = ("suspense", "clearing", "ask my accountant", "uncategorized")


def _line_amount(line: dict) -> float:
    try:
        return float(line.get("Amount", 0) or 0)  # SA-OK: dict access on QBO report JSON, no DB session in this module
    except (TypeError, ValueError):
        return 0.0


def _two_sided_modified_z_outliers(values: list[float]) -> dict[int, float]:
    """Return {index: modified_z} for values whose |modified z| >= the threshold.

    TWO-SIDED + robust (median + MAD), per DEC-CAR-C-7 / AP-FIN-06 — never the
    one-sided mean+pstdev. Returns {} when MAD is 0 (no spread → no outlier; never
    a fabricated flag) or when there are too few points to judge.
    """
    if len(values) < 3:
        return {}
    med = statistics.median(values)
    abs_devs = [abs(v - med) for v in values]
    mad = statistics.median(abs_devs)
    if mad == 0:
        return {}  # zero spread: no robust outlier; do NOT fall back to a fabricated flag
    out: dict[int, float] = {}
    for i, v in enumerate(values):
        modified_z = MAD_NORMAL_SCALE * (v - med) / mad
        if abs(modified_z) >= MODIFIED_Z_THRESHOLD:  # two-sided: both tails
            out[i] = round(modified_z, 3)
    return out


def journal_entry_structure(qbo_client: QBOClient, limit: int = 1000) -> dict:
    """Detect unusual-structure journal entries. Pure detector; no-data degrades.

    Returns a dict carrying ``available`` (False ⇒ the wrapper emits
    could_not_verify), the per-signal flags, and the flagged entries. NEVER
    fabricates a clean pass on a degraded source.
    """
    try:
        # READ-ONLY SELECT — no GL write. # QBO-OK: read-only-select-not-a-je-write
        resp = qbo_client.query(f"SELECT * FROM JournalEntry STARTPOSITION 1 MAXRESULTS {limit}")
    except Exception as e:  # honest degradation — a failed fetch is NOT a clean pass
        logger.warning("[je_structure] query failed: %s", type(e).__name__)
        return {"available": False, "reason": "journal-entry query failed", "entries": [], "anomalies": []}

    entries = resp.get("QueryResponse", {}).get("JournalEntry", [])
    if not entries:
        # No entries is a genuinely-evaluated empty population for a structure
        # check: nothing structurally unusual. A clean NO_HIT, not could-not-verify
        # — the query succeeded and returned an empty set.
        return {"available": True, "entries": [], "anomalies": [], "total_entries": 0}

    parsed: list[dict] = []
    amounts: list[float] = []
    for je in entries:
        lines = je.get("Line", []) or []
        debit = sum(_line_amount(ln) for ln in lines
                    if ln.get("JournalEntryLineDetail", {}).get("PostingType") == "Debit")
        credit = sum(_line_amount(ln) for ln in lines
                     if ln.get("JournalEntryLineDetail", {}).get("PostingType") == "Credit")
        account_names = [
            (ln.get("JournalEntryLineDetail", {}).get("AccountRef", {}) or {}).get("name", "")
            for ln in lines
        ]
        je_total = max(debit, credit)
        amounts.append(je_total)
        parsed.append({
            "id": je.get("Id"),
            "doc_number": je.get("DocNumber", ""),
            "txn_date": je.get("TxnDate", ""),
            "create_time": (je.get("MetaData", {}) or {}).get("CreateTime", ""),
            "private_note": je.get("PrivateNote", ""),
            "debit_total": round(debit, 2),
            "credit_total": round(credit, 2),
            "amount": round(je_total, 2),
            "account_names": account_names,
        })

    # Amount-outlier flags — TWO-SIDED modified z (median + MAD), AP-FIN-06.
    z_outliers = _two_sided_modified_z_outliers(amounts)

    # Duplicate detection: same (amount, account-set) signature on >1 entry.
    sig_counts: dict[tuple, int] = {}
    for p in parsed:
        sig = (p["amount"], tuple(sorted(n for n in p["account_names"] if n)))
        sig_counts[sig] = sig_counts.get(sig, 0) + 1  # SA-OK: bounded in-memory dict over the fetched page, not a DB query

    anomalies: list[dict] = []
    for i, p in enumerate(parsed):
        signals: list[str] = []

        # 1. Out-of-balance (debits != credits) — a hard structural defect.
        if abs(p["debit_total"] - p["credit_total"]) > BALANCE_TOLERANCE:
            signals.append("unbalanced")

        # 2. Round-number amount (exact multiple of the modulo, non-zero).
        if p["amount"] > 0 and p["amount"] % ROUND_NUMBER_MODULO == 0:
            signals.append("round_number")

        # 3. Weekend posting (TxnDate falls on Sat/Sun).
        if p["txn_date"]:
            try:
                d = datetime.strptime(p["txn_date"], "%Y-%m-%d").date()
                if d.weekday() >= 5:  # 5=Sat, 6=Sun
                    signals.append("weekend")
                # 4. Backdated: posted (CreateTime) well after the TxnDate.
                ct = p["create_time"]
                if ct:
                    try:
                        created = datetime.fromisoformat(ct.replace("Z", "+00:00")).date()
                        if created > d and (created - d).days >= 30:
                            signals.append("backdated")
                    except ValueError:
                        pass
            except ValueError:
                pass

        # 5. Suspense / clearing / uncategorized account hit.
        if any(
            marker in (name or "").lower()
            for name in p["account_names"]
            for marker in SUSPENSE_ACCOUNT_MARKERS
        ):
            signals.append("suspense_account")

        # 6. Duplicate signature.
        sig = (p["amount"], tuple(sorted(n for n in p["account_names"] if n)))
        if sig_counts.get(sig, 0) > 1 and p["amount"] > 0:
            signals.append("duplicate")

        # 7. Amount outlier (two-sided robust z).
        if i in z_outliers:
            signals.append("amount_outlier")

        if signals:
            flagged = dict(p)
            flagged["signals"] = signals
            if i in z_outliers:
                flagged["modified_z"] = z_outliers[i]
            anomalies.append(flagged)

    return {
        "available": True,
        "total_entries": len(parsed),
        "anomalies": anomalies,
        "method": "two-sided modified z (median+MAD, threshold 3.5) + structural signals",
    }

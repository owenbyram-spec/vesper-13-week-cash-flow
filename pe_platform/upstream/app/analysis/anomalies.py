"""Transaction-amount anomaly detector — TWO-SIDED modified z-score (median + MAD).

One-shot cutover (plan 20260711-peregrine-finance-os, FOS-1 AC-1.5) from the legacy
one-sided ``mean + pstdev`` z-score to the robust statistical shape already proven in
app/analysis/je_structure.py (DEC-CAR-C-7 / AP-FIN-06, financial-modeling-correctness
SKILL principle 7). The legacy path is REMOVED, not flag-gated:

- ``mean + pstdev`` lets the very outliers being hunted inflate sigma and mask
  themselves (heavy-tailed transaction amounts). Median + MAD are robust to the
  outliers under test.
- A one-sided ``z >= t`` structurally misses the negative tail; fraud and data
  errors live on BOTH tails, so we flag ``abs(modified_z) >= threshold``.
- ``z_score`` in each returned anomaly is therefore now SIGNED: negative means a
  low-side (unusually small) amount.

NAMED GAP (surfaced, not silently changed): Invoice and Purchase amounts are still
POOLED into one distribution, as the legacy detector did (SKILL principle 7 prefers
per-population stats; pooling here is a standing product choice — changing the
population split would change every anomaly fingerprint and is outside this
cutover's scope).
"""

from __future__ import annotations

import statistics
from datetime import date

from ..qbo_client import QBOClient

# ── named, auditable thresholds — mirror app/analysis/je_structure.py:43-46 ─────
# Robust modified-z cutoff (median+MAD); Iglewicz-Hoaglin standard.
MODIFIED_Z_THRESHOLD = 3.5
# 0.6745 = the 0.75 quantile of the standard normal; it scales MAD so the modified
# z-score is comparable to a classic z under normality (Iglewicz & Hoaglin 1993).
MAD_NORMAL_SCALE = 0.6745
# Below this many points there is no meaningful robust spread to judge against
# (je_structure._two_sided_modified_z_outliers uses the same floor).
MIN_POPULATION = 3


def transaction_anomalies(
    qbo_client: QBOClient,
    limit: int = 1000,
    z_threshold: float = MODIFIED_Z_THRESHOLD,
    txn_date_from: str | None = None,
) -> dict:
    """Flag Invoice/Purchase transactions whose amount is a two-sided robust outlier.

    ``z_threshold`` is on the MODIFIED-z scale (median + MAD, scaled by 0.6745),
    default 3.5 — NOT the ordinary-z scale of the removed legacy detector; a caller
    carrying a legacy-tuned 2.5 must re-judge it (2.5 modified-z is substantially
    noisier than the 3.5 standard).

    ``txn_date_from`` (``YYYY-MM-DD``, AC-A2 plan 20260716-five-domain-remediation):
    the open-period lower bound — transactions dated before it (i.e. in a
    soft-CLOSED period) never enter the population, so the anomaly's row period
    and its evidence dates agree. The value is strictly re-parsed as an ISO date
    before it reaches the QBO query (a malformed bound raises, never interpolates).

    Returns a dict with ``median_amount`` / ``mad_amount`` (the robust stats),
    ``transactions`` (the full pulled population) and ``anomalies`` — each anomaly
    carries a SIGNED ``z_score`` (negative = low-side outlier), sorted by |z| desc.
    MAD == 0 (zero spread) yields no outliers — never a fabricated flag.
    """
    txns = []

    date_clause = ""
    if txn_date_from:
        # Strict ISO re-derivation: only a genuine date ever reaches the query text.
        date_clause = f" WHERE TxnDate >= '{date.fromisoformat(txn_date_from).isoformat()}'"

    inv_data = qbo_client.query(f"SELECT * FROM Invoice{date_clause} STARTPOSITION 1 MAXRESULTS {limit}")
    for inv in inv_data.get("QueryResponse", {}).get("Invoice", []):
        amt = inv.get("TotalAmt", 0.0)
        txns.append(
            {
                "type": "Invoice",
                "id": inv.get("Id"),
                "name": inv.get("CustomerRef", {}).get("name", "Unknown"),
                "date": inv.get("TxnDate"),
                "amount": amt,
            }
        )

    pur_data = qbo_client.query(f"SELECT * FROM Purchase{date_clause} STARTPOSITION 1 MAXRESULTS {limit}")
    for p in pur_data.get("QueryResponse", {}).get("Purchase", []):
        amt = p.get("TotalAmt", 0.0)
        txns.append(
            {
                "type": "Purchase",
                "id": p.get("Id"),
                "name": p.get("EntityRef", {}).get("name", "Unknown"),
                "date": p.get("TxnDate"),
                "amount": amt,
            }
        )

    amounts = [t["amount"] for t in txns]
    if len(amounts) < MIN_POPULATION:
        return {
            "message": "Not enough transactions to compute anomalies.",
            "transactions": txns,
            "anomalies": [],
        }

    median_amt = statistics.median(amounts)
    mad_amt = statistics.median([abs(a - median_amt) for a in amounts])

    anomalies = []
    if mad_amt > 0:
        for t in txns:
            modified_z = MAD_NORMAL_SCALE * (t["amount"] - median_amt) / mad_amt
            if abs(modified_z) >= z_threshold:  # two-sided: both tails
                t_with_z = dict(t)
                t_with_z["z_score"] = round(modified_z, 3)
                anomalies.append(t_with_z)
    # mad_amt == 0: zero spread -> no robust outlier; do NOT fall back to a
    # fabricated flag (je_structure convention).

    return {
        "median_amount": median_amt,
        "mad_amount": mad_amt,
        "z_threshold": z_threshold,
        "method": "two_sided_modified_z",
        "transactions": txns,
        "anomalies": sorted(anomalies, key=lambda x: abs(x["z_score"]), reverse=True),
    }

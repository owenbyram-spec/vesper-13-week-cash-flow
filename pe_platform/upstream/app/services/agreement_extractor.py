"""
app/services/agreement_extractor.py
──────────────────────────────────────
Extracts text from uploaded agreement documents (PDF, DOCX, TXT),
then uses GPT-4o to parse:
  - Cap table entries (investors, share class, % ownership, amount invested)
  - Economic terms (management fees, carried interest, hurdle rate,
    distribution waterfall, preferred returns, clawback provisions)
  - Plain-English summary of the agreement structure

Results are stored in CompanyAgreement and optionally auto-populate CapTableEntry.
"""
from __future__ import annotations

import io
import json  # noqa: F401
import logging
from typing import TypedDict
from typing import Any, Dict, List, Optional  # noqa: F401

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Text Extraction
# ─────────────────────────────────────────────────────────────────────────────

def extract_text_from_pdf(file_bytes: bytes) -> str:
    """Extract all text from a PDF using pdfplumber (best table/layout handling)."""
    try:
        import pdfplumber
        pages = []
        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
            for page in pdf.pages:
                text = page.extract_text()
                if text:
                    pages.append(text)
        return "\n\n".join(pages)
    except ImportError:
        # Fallback: PyPDF2
        try:
            import PyPDF2
            reader = PyPDF2.PdfReader(io.BytesIO(file_bytes))
            return "\n\n".join(
                page.extract_text() or "" for page in reader.pages
            )
        except ImportError:
            logger.warning("Neither pdfplumber nor PyPDF2 installed — cannot extract PDF text")
            return ""


def extract_text_from_docx(file_bytes: bytes) -> str:
    """Extract text from a DOCX file."""
    try:
        from docx import Document
        doc = Document(io.BytesIO(file_bytes))
        paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
        # Also grab table cells
        for table in doc.tables:
            for row in table.rows:
                row_text = " | ".join(cell.text.strip() for cell in row.cells if cell.text.strip())
                if row_text:
                    paragraphs.append(row_text)
        return "\n".join(paragraphs)
    except ImportError:
        logger.warning("python-docx not installed — cannot extract DOCX text")
        return ""


def extract_text(file_bytes: bytes, filename: str) -> str:
    """Route to the correct extractor based on file extension."""
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    if ext == "pdf":
        return extract_text_from_pdf(file_bytes)
    elif ext in ("docx", "doc"):
        return extract_text_from_docx(file_bytes)
    elif ext in ("txt", "md"):
        return file_bytes.decode("utf-8", errors="replace")
    else:
        # Try PDF first, then DOCX, then raw text
        text = extract_text_from_pdf(file_bytes)
        if not text:
            text = extract_text_from_docx(file_bytes)
        if not text:
            text = file_bytes.decode("utf-8", errors="replace")
        return text


# ─────────────────────────────────────────────────────────────────────────────
# LLM Extraction
# ─────────────────────────────────────────────────────────────────────────────

# PLAN-EXEMPT: LBO field-bug wave-6 (Owen 2026-06-29) — add the acquisition agreement types so an
# Asset/Stock Purchase or Merger agreement is typed correctly here (it was forced into Operating/
# Other), keeping agreement_type coherent with apa_terms.is_acquisition_agreement.
# PLAN-EXEMPT: chunk C1 of ratified plan 20260714-wave2-7b-lease-extraction (marker RATIFICATION-2026-07-14.md)
#   — adds the lease-extraction lane (is_lease_agreement + per-criterion ASC-842 classification + the
#   E2 non-lease-component flag), mirroring loan_terms; extraction stays PURE, no LeaseSchedule write.
# PLAN-EXEMPT: chunk C1 of ratified plan 20260714-wave2-7b-lease-extraction (marker RATIFICATION-2026-07-14.md)
SYSTEM_PROMPT = """You are a sophisticated legal and financial analyst specializing in company agreements
(LLC Operating Agreements, Limited Partnership Agreements, Shareholders Agreements, Advisory Agreements,
asset/stock purchase agreements, merger agreements, and real-property / equipment / vehicle lease agreements).

Given the text of a company agreement, extract the following information and return it as a single valid JSON object:

{
  "agreement_type": "LLC Operating Agreement | LP Agreement | Shareholders Agreement | Management Agreement | Asset Purchase Agreement | Stock Purchase Agreement | Merger Agreement | Loan Agreement | Lease Agreement | Other",
  "company_name": "...",
  "cap_table": [
    {
      "investor_name": "...",
      "investor_type": "individual | entity | fund",
      "share_class": "Common | Preferred A | SAFE | Note | LP Interest | GP Interest | Member Units | Other",
      "ownership_pct": <number 0-100 or null if not stated>,
      "shares_or_units": <number or null>,
      "total_units_outstanding": <number or null>,
      "investment_amount": <number in USD or null>,
      "role": "GP | LP | Managing Member | Member | Advisor | Other"
    }
  ],
  "economics": {
    "management_fee_pct": <number or null>,
    "management_fee_basis": "committed capital | invested capital | NAV | AUM | revenue | flat fee | null",
    "management_fee_amount_annual": <number or null>,
    "carried_interest_pct": <number or null>,
    "hurdle_rate_pct": <number or null>,
    "preferred_return_pct": <number or null>,
    "catch_up_pct": <number or null>,
    "distribution_waterfall": "American | European | Deal-by-deal | Pro-rata | Custom",
    "distribution_priority": ["Return of Capital", "Preferred Return X%", "Catch-up", "Carried Interest XX/YY split"],
    "profit_split": <string description of how profits are split>,
    "loss_allocation": <string or null>,
    "clawback_provision": <true | false | null>,
    "key_person_clause": <true | false | null>,
    "vesting_schedule": <string description or null>,
    "other_fees": [{"name": "...", "amount_or_pct": "..."}]
  },
  "loan_terms": {
    "is_loan_agreement": <true ONLY for a credit / loan / debt / promissory-note instrument; else false>,
    "principal":         {"value": <face/principal amount in USD or null>, "confidence": <0.0-1.0>, "source_quote": "<verbatim clause the value came from, or null>"},
    "interest_rate":     {"value": <all-in annual rate as a DECIMAL e.g. 0.08 for 8%, or null>, "confidence": <0.0-1.0>, "source_quote": "..."},
    "rate_type":         {"value": "fixed | floating | null", "confidence": <0.0-1.0>, "source_quote": "..."},
    "base_rate":         {"value": <floating base/SOFR as a DECIMAL, or null — floating only>, "confidence": <0.0-1.0>, "source_quote": "..."},
    "spread":            {"value": <spread over base as a DECIMAL e.g. 0.025 for 250bps, or null — floating only>, "confidence": <0.0-1.0>, "source_quote": "..."},
    "maturity_date":     {"value": "<YYYY-MM-DD or null>", "confidence": <0.0-1.0>, "source_quote": "..."},
    "amortization_type": {"value": "straight_line | bullet | pik | null", "confidence": <0.0-1.0>, "source_quote": "..."},
    "payment_frequency": {"value": "monthly | quarterly | semiannual | annual | null", "confidence": <0.0-1.0>, "source_quote": "..."},
    "balloon_pct":       {"value": <fraction 0-1 of principal due as a balloon/bullet at maturity, or null>, "confidence": <0.0-1.0>, "source_quote": "..."}
  },
  "apa_terms": {
    "is_acquisition_agreement": <true ONLY for an asset/stock/equity/share purchase or merger agreement; else false>,
    "equity_pct_acquired": {"value": <fraction 0-1 of the target's equity being acquired, or null>, "confidence": <0.0-1.0>, "source_quote": "..."},
    "cash_consideration":  {"value": <the CASH leg of the consideration paid in USD — never a folded total, or null>, "confidence": <0.0-1.0>, "source_quote": "..."},
    "seller_note":         {"value": <consideration delivered as a seller / promissory note payable to the seller in USD, or null>, "confidence": <0.0-1.0>, "source_quote": "..."},
    "equity_rollover":     {"value": <consideration delivered as equity in the buyer/parent (preferred equity, rollover units, stock consideration to the seller) in USD, or null>, "confidence": <0.0-1.0>, "source_quote": "..."},
    "equity_value":        {"value": <total 100% equity value in USD if stated outright, or null>, "confidence": <0.0-1.0>, "source_quote": "..."},
    "enterprise_value":    {"value": <total enterprise value / purchase price in USD if stated outright, or null>, "confidence": <0.0-1.0>, "source_quote": "..."},
    "assumed_debt":        {"value": <debt assumed by the buyer in USD, or null>, "confidence": <0.0-1.0>, "source_quote": "..."},
    "cash_acquired":       {"value": <cash on the target's balance sheet acquired in USD, or null>, "confidence": <0.0-1.0>, "source_quote": "..."},
    "transaction_fees":    {"value": <advisory/transaction fees in USD, or null>, "confidence": <0.0-1.0>, "source_quote": "..."},
    "closing_date":        {"value": "<YYYY-MM-DD or null>", "confidence": <0.0-1.0>, "source_quote": "..."},
    "target_name":         {"value": "<company being acquired, or null>", "confidence": <0.0-1.0>, "source_quote": "..."},
    "buyer_name":          {"value": "<acquirer, or null>", "confidence": <0.0-1.0>, "source_quote": "..."}
  },
  "lease_terms": {
    "is_lease_agreement": <true ONLY for a lease / rental instrument (real-property, equipment, or vehicle lease); else false>,
    "payment_amount":       {"value": <the LEASE-COMPONENT fixed periodic payment in USD — NET of any bundled CAM/tax/insurance/service; null if not stated>, "confidence": <0.0-1.0>, "source_quote": "<verbatim clause the value came from, or null>"},
    "term_months":          {"value": <lease term in whole months (integer >= 1), or null>, "confidence": <0.0-1.0>, "source_quote": "..."},
    "commencement_date":    {"value": "<YYYY-MM-DD or null>", "confidence": <0.0-1.0>, "source_quote": "..."},
    "payment_timing":       {"value": "advance | arrears | null", "confidence": <0.0-1.0>, "source_quote": "..."},
    "discount_rate_annual": {"value": <annual rate as a DECIMAL in (0,1), e.g. 0.06 for 6%, or null>, "confidence": <0.0-1.0>, "source_quote": "..."},
    "rate_basis":           {"value": "ibr | risk_free | null", "confidence": <0.0-1.0>, "source_quote": "..."},
    "initial_direct_costs": {"value": <USD >= 0, or null>, "confidence": <0.0-1.0>, "source_quote": "..."},
    "incentives":           {"value": <lessor incentives / TI allowance in USD >= 0, or null>, "confidence": <0.0-1.0>, "source_quote": "..."},
    "prepaid_amount":       {"value": <prepaid rent in USD >= 0, or null>, "confidence": <0.0-1.0>, "source_quote": "..."},
    "short_term_exempt":    {"value": <true | false | null — true ONLY if term <= 12 months AND no purchase option reasonably certain>, "confidence": <0.0-1.0>, "source_quote": "..."},
    "escalation_steps":     {"value": [{"start_month": <int in 2..term_months>, "payment": <USD>}], "confidence": <0.0-1.0>, "source_quote": "..."},
    "classification":       {"value": "operating | finance | null — your PROPOSED ASC 842 verdict; the human confirms it", "confidence": <0.0-1.0>, "source_quote": "..."},
    "classification_criteria": {
      "transfers_ownership":                {"value": <true | false | null>, "confidence": <0.0-1.0>, "source_quote": "..."},
      "purchase_option_reasonably_certain": {"value": <true | false | null>, "confidence": <0.0-1.0>, "source_quote": "..."},
      "term_major_part_of_life":            {"value": <true | false | null>, "confidence": <0.0-1.0>, "source_quote": "..."},
      "pv_substantially_all_fair_value":    {"value": <true | false | null>, "confidence": <0.0-1.0>, "source_quote": "..."},
      "specialized_asset":                  {"value": <true | false | null>, "confidence": <0.0-1.0>, "source_quote": "..."}
    },
    "non_lease_components": [
      {"name": "<CAM | maintenance | property tax | insurance | service | ...>", "amount": <periodic USD amount or null>, "source_quote": "<verbatim clause — REQUIRED; this is the evidence the reviewer splits the payment on>"}
    ]
  },
  "summary": "Plain English 3-5 sentence summary of what this agreement establishes, who the parties are, and how economics flow."
}

Rules:
- Be precise — extract exact numbers when stated; use null when not mentioned
- If ownership percentages add up, verify they total ~100%
- For management fee, convert flat dollar amounts to annual if monthly/quarterly stated
- loan_terms: set "is_loan_agreement" true ONLY for a credit/loan/debt/promissory instrument. If false,
  set every loan_terms field "value" to null with "confidence" 0.0. Rates are DECIMALS (8% -> 0.08), never percents.
- loan_terms confidence: how EXPLICITLY the term is stated — 1.0 = stated verbatim and unambiguous;
  0.5 = inferred or ambiguous; 0.0 = not found. "source_quote" is the verbatim text you read the value
  from (null when the value is inferred rather than quoted). NEVER guess a number with high confidence.
- apa_terms: set "is_acquisition_agreement" true ONLY for an asset/stock/equity/share purchase or merger
  agreement. If false, set every apa_terms field "value" to null with confidence 0.0. Percentages are
  DECIMALS (80% -> 0.80), dollar amounts in USD. Same confidence/source_quote discipline as loan_terms.
- apa_terms consideration split: when the purchase price splits into legs (e.g. "$4,500,000 in cash plus
  $1,500,000 of Preferred Equity" or "cash plus a promissory note"), extract EACH leg separately —
  "cash_consideration" is the CASH leg ONLY, the note leg goes to "seller_note", the equity/stock leg to
  "equity_rollover", each with its own verbatim source_quote. NEVER fold a note or equity leg into
  cash_consideration, and NEVER decide the note/equity leg's treatment (debt vs rollover) — record the
  amounts + quotes; that classification is a human's call.
- lease_terms: set "is_lease_agreement" true ONLY for a lease / rental instrument (real-property, equipment,
  or vehicle lease). If false, set every lease_terms field "value" to null with "confidence" 0.0 and
  "non_lease_components" to []. Rates are DECIMALS (6% -> 0.06), never percents — a rate you cannot tell is
  percent-or-decimal gets a null value with low confidence, NEVER a guess.
- lease_terms classification: extract the five ASC 842-10-25-2 criteria PER-CRITERION, each with its own
  confidence + source_quote, plus a PROPOSED "classification" — never a bare finance/operating verdict without
  the per-criterion evidence. ANY ONE criterion true makes it a finance lease. When the document does not state
  a criterion, use null (NOT false) so the human reviewer sees the gap. NEVER auto-decide classification with
  high confidence from silence — it is judgment the human confirms, not a fact.
- lease_terms "payment_amount" is the LEASE-COMPONENT payment ONLY. If the agreement bundles non-lease
  components (common-area maintenance, property tax, insurance, maintenance, or service), extract the lease
  payment NET of them and FLAG each bundled charge in "non_lease_components" with a verbatim source_quote
  (ASC 842-10-15-28+). A single gross payment overstates the lease liability — surface the split; do NOT
  silently fold CAM/tax into payment_amount.
- lease_terms confidence follows the loan_terms discipline (1.0 verbatim, 0.5 inferred, 0.0 not found). US GAAP
  ASC 842 has NO low-value expedient — model ONLY "short_term_exempt"; do not invent a low-value exemption.
- Return ONLY the JSON object, no markdown, no prose outside the JSON
"""
# PLAN-EXEMPT: chunk C1 of ratified plan 20260714-wave2-7b-lease-extraction (marker RATIFICATION-2026-07-14.md)
#   — the lease_terms extraction block above mirrors loan_terms; the pure ReviewedLeaseTerms mapper is below.


def analyze_with_llm(text: str, company_name: str = "") -> Dict[str, Any]:
    """Send agreement text to the configured LLM and parse the structured extraction."""
    # With Gemini 2.5 Pro's 1M-token context we can pass much more text.
    # Truncate at 400k chars (~100k tokens) as a conservative safety limit.
    truncated = text[:400_000]
    if len(text) > 400_000:
        truncated += "\n\n[... document truncated for length ...]"

    try:
        from ..services.llm import analysis_llm
        result = analysis_llm.predict_json(
            system_prompt=SYSTEM_PROMPT,
            user_prompt=f"Company: {company_name}\n\nAgreement Text:\n{truncated}",
            temperature=0.1,
        )
        if result is None:
            return {"error": "LLM returned no response"}
        return result
    except Exception as e:
        logger.error(f"[AgreementExtractor] LLM call failed: {e}")
        raise

# Keep old name for backwards compatibility
analyze_with_gpt4o = analyze_with_llm


# ─────────────────────────────────────────────────────────────────────────────
# Cap Table Entry Creation
# ─────────────────────────────────────────────────────────────────────────────

def create_cap_table_entries(
    db,
    company_id: str,
    cap_table_data: List[Dict],
    replace_existing: bool = False,
) -> int:
    """
    Take the extracted cap table list and upsert CapTableEntry records.
    Returns the number of entries created/updated.
    """
    from ..models import CapTableEntry

    if replace_existing:
        db.query(CapTableEntry).filter(CapTableEntry.company_id == company_id).delete()
        db.flush()

    # Compute total units if available
    total_units = next(
        (float(e.get("total_units_outstanding", 0) or 0) for e in cap_table_data if e.get("total_units_outstanding")),
        0.0,
    )

    count = 0
    for entry in cap_table_data:
        if not entry.get("investor_name"):
            continue
        shares = float(entry.get("shares_or_units", 0) or 0)
        pct = float(entry.get("ownership_pct", 0) or 0)
        invested = float(entry.get("investment_amount", 0) or 0)

        row = CapTableEntry(
            company_id=company_id,
            investor_name=entry.get("investor_name", "Unknown"),
            investor_type=entry.get("investor_type", "individual"),
            share_class=entry.get("share_class", "Common"),
            shares_owned=shares,
            total_shares_outstanding=total_units or (shares / (pct / 100) if pct > 0 else 0),
            ownership_pct=pct / 100 if pct > 1 else pct,  # normalize to 0-1
            investment_amount=invested,
            notes=f"Role: {entry.get('role', '')} — extracted from uploaded agreement",
        )
        db.add(row)
        count += 1

    db.flush()
    return count


# ─────────────────────────────────────────────────────────────────────────────
# Loan-terms → LboDebtTranche field mapping (the confirm-endpoint bridge)
# ─────────────────────────────────────────────────────────────────────────────

# Maps user-REVIEWED loan terms onto the LboDebtTranche columns the live grid seam
# actually reads (lbo.py:1844-1851 reads current_balance / sofr_rate / spread_bps /
# amort_years / bullet_pct / pik_flag — NOT the legacy funded_amount/interest_rate).
# This is the field-assignment bridge, NOT financial math; the amortization itself is
# owned by compute_debt_schedule. Fails loud on a missing principal/rate rather than
# defaulting a literal (financial-modeling: never fabricate the basis of a number).
_AMORT_TYPES = {"straight_line", "bullet", "pik"}


class ReviewedLoanTerms(TypedDict, total=False):
    """User-reviewed, flat loan terms (post review/edit) — the confirm-endpoint input."""
    principal: float | None
    rate_type: str | None            # "fixed" | "floating"
    interest_rate: float | None      # all-in annual decimal (fixed)
    base_rate: float | None          # floating base/SOFR decimal
    spread: float | None             # floating spread decimal (0.025 == 250bps)
    amortization_type: str | None    # "straight_line" | "bullet" | "pik"
    amort_years: int | None          # straight_line only
    balloon_pct: float | None        # bullet only (0-1 fraction)
    maturity_date: str | None        # ISO date
    start_date: str | None           # ISO date


class TrancheFields(TypedDict):
    """The LboDebtTranche column kwargs the live grid seam reads (the NEW fields, not legacy)."""
    current_balance: float
    sofr_rate: float
    spread_bps: int
    rate_type: str
    amort_years: int | None
    bullet_pct: float
    pik_flag: bool
    maturity_date: str | None
    start_date: str | None


def loan_terms_to_tranche_fields(reviewed: ReviewedLoanTerms) -> TrancheFields:
    """
    Translate user-reviewed loan terms into LboDebtTranche column kwargs.

    Raises ValueError on a missing/invalid principal or rate — the confirm endpoint
    surfaces that as a 400 rather than writing a tranche off a fabricated basis.
    """
    principal = reviewed.get("principal")
    if principal is None or float(principal) <= 0:
        raise ValueError("principal is required and must be > 0 to create a debt tranche")

    rate_type = (reviewed.get("rate_type") or "fixed").lower()
    if rate_type == "floating":
        base = reviewed.get("base_rate")
        spread = reviewed.get("spread")
        if base is None or spread is None:
            raise ValueError("floating loan requires both base_rate and spread")
        sofr_rate = float(base)
        spread_bps = int(round(float(spread) * 10000))
    else:
        rate = reviewed.get("interest_rate")
        if rate is None:
            raise ValueError("fixed loan requires interest_rate")
        # Fixed rate rides entirely in sofr_rate; spread_bps=0 so the seam's
        # base+spread math (lbo.py:1845) reproduces the fixed rate exactly.
        sofr_rate = float(rate)
        spread_bps = 0

    amort_type = (reviewed.get("amortization_type") or "straight_line").lower()
    if amort_type not in _AMORT_TYPES:
        raise ValueError(f"amortization_type must be one of {sorted(_AMORT_TYPES)}")

    amort_years: int | None = None
    bullet_pct = 0.0
    pik_flag = False
    if amort_type == "straight_line":
        ay = reviewed.get("amort_years")
        if ay is None or int(ay) <= 0:
            raise ValueError("straight_line amortization requires amort_years > 0")
        amort_years = int(ay)
    elif amort_type == "bullet":
        # Whole principal due at maturity unless a partial balloon fraction is given.
        bp = reviewed.get("balloon_pct")
        bullet_pct = float(bp) if bp is not None else 1.0
    elif amort_type == "pik":
        pik_flag = True

    return {
        "current_balance": float(principal),
        "sofr_rate": sofr_rate,
        "spread_bps": spread_bps,
        "rate_type": "floating" if rate_type == "floating" else "fixed",
        "amort_years": amort_years,
        "bullet_pct": bullet_pct,
        "pik_flag": pik_flag,
        "maturity_date": reviewed.get("maturity_date"),
        "start_date": reviewed.get("start_date"),
    }


class ReviewedApaTerms(TypedDict, total=False):
    """User-reviewed acquisition terms (post review/edit) — the APA confirm-endpoint input."""
    equity_pct_acquired: float | None   # fraction 0-1 of equity acquired
    cash_consideration: float | None    # cash paid for that stake (USD)
    seller_note: float | None           # consideration delivered as a seller note (USD)
    equity_rollover: float | None       # consideration delivered as equity in the buyer (USD)
    equity_value: float | None          # total 100% equity value (USD) if stated
    enterprise_value: float | None      # total EV / purchase price (USD) if stated
    assumed_debt: float | None
    cash_acquired: float | None
    transaction_fees: float | None      # advisory/transaction fees (USD)
    closing_date: str | None            # ISO date


class DriverSeedFields(TypedDict, total=False):
    """The LboDriver.driver_data keys an APA confirm seeds (read by compute_sources_uses)."""
    entry_enterprise_value: float
    transaction_fees_pct: float
    closing_date: str


def target_net_debt(reviewed: ReviewedApaTerms) -> float:
    """The TARGET's net debt the agreement states: assumed debt − cash acquired.

    This is the only net debt that converts an equity price into an enterprise
    value. The buyer's acquisition financing (the scenario's confirmed tranches)
    is a SOURCE that funds the price — it never adds to it."""
    return round(
        float(reviewed.get("assumed_debt") or 0.0) - float(reviewed.get("cash_acquired") or 0.0), 2
    )


def apa_terms_stated_net_debt(reviewed: ReviewedApaTerms) -> bool:
    """Whether the agreement states the target's debt or acquired cash at all."""
    return reviewed.get("assumed_debt") is not None or reviewed.get("cash_acquired") is not None


def apa_terms_to_driver_fields(reviewed: ReviewedApaTerms) -> DriverSeedFields:
    """Translate user-reviewed APA terms into LboDriver.driver_data seed kwargs.

    Resolves the entry enterprise value from the strongest signal available:
      1. an explicit enterprise_value, else
      2. an explicit equity_value + the target's net debt, else
      3. implied 100% equity (cash_consideration / equity_pct_acquired) + the
         target's net debt, else
      4. the TOTAL DOCUMENTED CONSIDERATION — cash + the stated split legs
         (seller_note / equity_rollover) + the target's net debt. The asset-deal
         shape: an APA that enumerates its consideration into legs is stating the
         full purchase price.

    The target's net debt is ``assumed_debt − cash_acquired`` as the agreement
    states it (:func:`target_net_debt`). The buyer's acquisition financing never
    enters the price on ANY branch: it is a Source in the S&U, so leverage lowers
    the sponsor's equity check instead of inflating EV, the entry multiple and
    goodwill.

    Raises ValueError when no equity/EV basis can be resolved — the confirm endpoint
    surfaces that as a 400 rather than seeding a fabricated EV (financial-modeling:
    never fabricate the basis of a number). A cash figure ALONE (no % acquired, no
    split legs) still refuses: cash for an unknown stake cannot imply a total price."""
    ev = reviewed.get("enterprise_value")
    equity = reviewed.get("equity_value")
    cash = reviewed.get("cash_consideration")
    pct = reviewed.get("equity_pct_acquired")
    split_legs = [
        float(reviewed[key])  # type: ignore[literal-required]
        for key in ("seller_note", "equity_rollover")
        if reviewed.get(key) is not None and float(reviewed[key]) > 0  # type: ignore[literal-required]
    ]
    net_debt = target_net_debt(reviewed)
    if ev is not None and float(ev) > 0:
        entry_ev = float(ev)
    elif equity is not None and float(equity) > 0:
        entry_ev = float(equity) + net_debt
    elif cash is not None and float(cash) > 0 and pct is not None and float(pct) > 0:
        entry_ev = float(cash) / float(pct) + net_debt   # implied 100% equity value
    elif cash is not None and float(cash) > 0 and split_legs:
        entry_ev = float(cash) + sum(split_legs) + net_debt
    else:
        raise ValueError(
            "APA must state an enterprise value, an equity value, a cash "
            "consideration together with the equity percentage acquired, or a "
            "cash consideration split into stated consideration legs "
            "(seller note / equity rollover)"
        )

    seed: DriverSeedFields = {"entry_enterprise_value": round(entry_ev, 2)}
    fees = reviewed.get("transaction_fees")
    if fees is not None and float(fees) > 0 and entry_ev > 0:
        seed["transaction_fees_pct"] = round(float(fees) / entry_ev, 6)
    closing = reviewed.get("closing_date")
    if closing:
        seed["closing_date"] = closing
    return seed


# ─────────────────────────────────────────────────────────────────────────────
# Documents → Sources & Uses: the composed-flow assembly helpers (AC-FRR-6)
# ─────────────────────────────────────────────────────────────────────────────
# PURE (no db, no persistence): these assemble the provenance-tagged S&U proposal
# from already-extracted terms and validate the composed confirm's legs BEFORE
# any write. The writers stay the two existing seams — create_or_update_loan_tranche
# (the ONLY tranche writer) and apa_terms_to_driver_fields (the ONE APA mapper) —
# never a parallel implementation.

# The apa_terms envelope keys the preview surfaces (each {value, confidence,
# source_quote}); CONSIDERATION_SPLIT_KEYS are the non-cash consideration legs
# whose debt-vs-rollover treatment is a HUMAN classification (D-FRR-3): the
# extraction records amount + quote, never the treatment.
APA_ENVELOPE_KEYS = (
    "equity_pct_acquired", "cash_consideration", "seller_note", "equity_rollover",
    "equity_value", "enterprise_value", "assumed_debt", "cash_acquired",
    "transaction_fees", "closing_date",
)
LOAN_ENVELOPE_KEYS = (
    "principal", "interest_rate", "rate_type", "base_rate", "spread",
    "maturity_date", "amortization_type", "payment_frequency", "balloon_pct",
)
CONSIDERATION_SPLIT_KEYS = ("seller_note", "equity_rollover")

# The reviewed-APA keys the driver mapper consumes (the confirm's apa leg).
# seller_note/equity_rollover feed ONLY the total-consideration EV basis here;
# their debt-vs-rollover S&U treatment stays the human consideration-leg call.
APA_REVIEWED_KEYS = (
    "equity_pct_acquired", "cash_consideration", "seller_note", "equity_rollover",
    "equity_value", "enterprise_value", "assumed_debt", "cash_acquired",
    "transaction_fees", "closing_date",
)


def provenanced_fields(
    terms: dict, keys: tuple, *, agreement_id: int, filename: str
) -> dict[str, dict]:
    """Flatten {value, confidence, source_quote} envelopes into provenance-tagged
    figures — every figure names its source document + verbatim quote (AC-FRR-6:
    no un-provenanced number reaches the preview). Null values are dropped."""
    out: dict[str, dict] = {}
    for key in keys:
        env = terms.get(key)  # SA-OK: dict.get on the in-memory extracted-terms JSON, not a DB query
        if not isinstance(env, dict):
            continue
        value = env.get("value")  # SA-OK: dict.get on the in-memory envelope dict, not a DB query
        if value is None:
            continue
        confidence = env.get("confidence")  # SA-OK: dict.get on the in-memory envelope dict, not a DB query
        out[key] = {
            "value": value,
            "confidence": float(confidence) if confidence is not None else None,
            "source_quote": env.get("source_quote"),
            "source_agreement_id": agreement_id,
            "source_filename": filename,
        }
    return out


def classify_deal_documents(rows: list) -> dict:
    """Sort a realm's analyzed agreements into the composed-flow proposal shape.

    ``rows`` is [(agreement_id, filename, extracted_economics)] — plain data, no
    ORM. Returns {"apa", "loans", "consideration_legs", "warnings"}: ONE
    acquisition agreement (callers pass rows newest-first; extras surface as a
    warning, never silently merged), every loan agreement, and one consideration
    leg per non-cash split field on the chosen APA — presented UNCLASSIFIED,
    because the debt-tranche-vs-equity-rollover call is the human's (D-FRR-3).
    """
    apa: dict | None = None
    loans: list = []
    legs: list = []
    warnings: list = []
    for agreement_id, filename, economics in rows:
        econ = economics or {}
        apa_terms = econ.get("apa_terms") or {}  # SA-OK: dict.get on the in-memory extracted-economics JSON, not a DB query
        loan_terms = econ.get("loan_terms") or {}  # SA-OK: dict.get on the in-memory extracted-economics JSON, not a DB query
        if apa_terms.get("is_acquisition_agreement") is True:  # SA-OK: dict.get on the in-memory apa_terms JSON, not a DB query
            if apa is None:
                fields = provenanced_fields(
                    apa_terms, APA_ENVELOPE_KEYS, agreement_id=agreement_id, filename=filename
                )
                apa = {"agreement_id": agreement_id, "filename": filename, "fields": fields}
                for key in CONSIDERATION_SPLIT_KEYS:
                    if key in fields:
                        legs.append({
                            "agreement_id": agreement_id,
                            "filename": filename,
                            "source_field": key,
                            "figure": fields[key],
                            "classification_options": ["debt_tranche", "equity_rollover"],
                        })
            else:
                warnings.append(
                    f"Multiple acquisition agreements found — using '{apa['filename']}'; "
                    f"'{filename}' was not included."
                )
        if loan_terms.get("is_loan_agreement") is True:  # SA-OK: dict.get on the in-memory loan_terms JSON, not a DB query
            # One loan entry per DISTINCT file (rows arrive newest-first, so the
            # freshest extraction wins) — duplicate agreement rows for the same
            # document otherwise double-count the proposed principal, and every
            # duplicate defaults to include=true in the confirm UI.
            if filename in {ln["filename"] for ln in loans}:
                warnings.append(
                    f"Duplicate analyzed agreement for '{filename}' skipped — the "
                    f"newest extraction is shown; delete the stale agreement row."
                )
                continue
            loans.append({
                "agreement_id": agreement_id,
                "filename": filename,
                "suggested_tranche_name": (filename.rsplit(".", 1)[0][:60] or "Term Loan"),
                "fields": provenanced_fields(
                    loan_terms, LOAN_ENVELOPE_KEYS, agreement_id=agreement_id, filename=filename
                ),
            })
    return {"apa": apa, "loans": loans, "consideration_legs": legs, "warnings": warnings}


def resolve_documented_ev(reviewed: ReviewedApaTerms) -> tuple[float | None, str | None]:
    """Resolve the DOCUMENTED entry EV through the ONE APA mapper (never a math fork).

    Returns (ev, basis): basis names which documented signal priced it — the
    explicit EV wins outright; the equity bases add the TARGET's net debt the
    agreement states (assumed debt − cash acquired), never the buyer's loans.
    Returns (None, None) when the documents state no EV basis — refused, never
    fabricated.
    """
    try:
        seed = apa_terms_to_driver_fields(reviewed)
    except ValueError:
        return None, None
    ev = reviewed.get("enterprise_value")
    equity = reviewed.get("equity_value")
    pct = reviewed.get("equity_pct_acquired")
    if ev is not None and float(ev) > 0:
        basis = "explicit_enterprise_value"
    elif equity is not None and float(equity) > 0:
        basis = "equity_value_plus_target_net_debt"
    elif pct is not None and float(pct) > 0:
        basis = "implied_equity_plus_target_net_debt"
    else:
        basis = "total_documented_consideration"
    return float(seed["entry_enterprise_value"]), basis


def evaluate_entry_gate(
    *,
    entry_pricing: str | None,
    documented_ev: float | None,
    stored_driver_ev: object,
    base_levels: object,
    y0_revenue: float,
    y0_ebitda: float,
    documented_fees: float = 0.0,
    estimated_buyer_financing: float = 0.0,
) -> dict:
    """Predict the engine's entry gate (engine_composition.py ~:539-546) for the
    composed flow's retire step (D-FRR-4): would retiring the stored S&U override
    leave the scenario unable to clear the gate's two conditions?

    Condition 1 (PRICING): after the composed confirm the pricing basis is the
    documented EV written to driver_data (or an EV already stored there) — the
    retired override no longer prices anything. Condition 2 (PROJECTION):
    base_levels or nonzero seq-0 revenue/EBITDA — untouched by the retire, but
    the gate refuses without it, so the warning names it rather than letting the
    confirm "succeed" into a refused engine run. The gate is only ENFORCED for
    unpriced books-start deals (entry_pricing == "unpriced_books_start").

    ``base_levels`` is the RAW persisted meta value (AC-6, Wave 5): the presence
    test mirrors the engine's read exactly — ``isinstance(_bl, dict)`` and a
    non-None ``_bl["levels"]`` (engine_composition ~:550) — so a malformed meta
    dict without "levels" predicts strand, matching the engine that would refuse.

    The projected sponsor-equity plug (AC-3, Wave 5): ``documented_ev +
    documented_fees − estimated_buyer_financing`` — the S&U residual the confirm
    would write (equity = total uses − buyer financing; the financing is the
    buyer's post-confirm tranche total, a Source). Surfaced with a
    ``would_be_negative`` flag so the confirm's negative-equity acknowledgment
    is an informed act; the confirm re-derives it off the real post-write
    tranche sum."""
    enforced = entry_pricing == "unpriced_books_start"
    pricing_ok = (documented_ev is not None and float(documented_ev) > 0) or (
        stored_driver_ev not in (None, "", 0)
    )
    # AC-6: the ENGINE's exact meta-shape read — a base_levels dict lacking "levels"
    # is ABSENT to the engine, so it must be absent to the strand prediction too.
    base_levels_present = isinstance(base_levels, dict) and base_levels.get("levels") is not None  # SA-OK: dict.get on the in-memory meta params, not a DB query
    projection_ok = bool(base_levels_present) or float(y0_revenue) != 0.0 or float(y0_ebitda) != 0.0
    projected_plug: float | None = None
    if documented_ev is not None and float(documented_ev) > 0:
        projected_plug = round(
            float(documented_ev) + float(documented_fees) - float(estimated_buyer_financing), 2
        )
    would_be_negative = projected_plug is not None and projected_plug < 0
    would_strand = enforced and not (pricing_ok and projection_ok)
    reason = None
    if would_strand:
        if not pricing_ok:
            reason = (
                "retiring the stored override would leave the scenario unpriced: the "
                "documents state no enterprise-value basis and no confirmed driver EV "
                "exists (the entry gate refuses an unpriced books-start deal)"
            )
        else:
            reason = (
                "the scenario has no projection base (no complete post-close fiscal year "
                "and no entry operating assumptions) — the entry gate will refuse the "
                "engine run even after the documents price the entry"
            )
    return {
        "enforced": enforced,
        "pricing_ok_after_confirm": pricing_ok,
        "projection_base_ok": projection_ok,
        "would_strand": would_strand,
        "reason": reason,
        "projected_equity_plug": projected_plug,
        "would_be_negative": would_be_negative,
    }


def plan_composed_tranche_writes(tranche_legs: list, consideration_legs: list) -> tuple[list, float]:
    """Validate EVERY leg of the composed confirm BEFORE the first write.

    Returns (planned_writes, rollover_total): planned_writes carry the kwargs for
    create_or_update_loan_tranche (THE one tranche writer executes them);
    rollover_total is the human-classified equity_rollover sum destined for
    driver_data["management_rollover_amount"]. Raises ValueError naming the
    offending leg so the confirm 400s before any tranche lands — never a
    half-written chain on a validatable defect. A debt-classified consideration
    leg must STATE its schedule (amortization_type + terms); nothing is assumed.
    """
    planned: list = []
    for leg in tranche_legs:
        reviewed = {
            key: leg.get(key)  # SA-OK: dict.get on the request-leg dict, not a DB query
            for key in (
                "principal", "rate_type", "interest_rate", "base_rate", "spread",
                "amortization_type", "amort_years", "balloon_pct", "maturity_date", "start_date",
            )
        }
        try:
            loan_terms_to_tranche_fields(reviewed)
        except ValueError as e:
            raise ValueError(f"loan tranche '{leg.get('tranche_name') or '?'}': {e}") from e
        planned.append({
            "agreement_id": leg["agreement_id"],
            "tranche_name": leg["tranche_name"],
            "gl_account_id": leg.get("gl_account_id"),
            "gl_account_name": leg.get("gl_account_name"),
            "tranche_id": leg.get("tranche_id"),
            "reviewed": reviewed,
        })
    rollover_total = 0.0
    for leg in consideration_legs:
        classification = leg["classification"]  # the HUMAN call (D-FRR-3) — never defaulted here
        if classification == "equity_rollover":
            rollover_total += float(leg["amount"])
            continue
        # debt_tranche: the human states the schedule too — no silent bullet default.
        if not leg.get("amortization_type"):  # SA-OK: dict.get on the request-leg dict, not a DB query
            raise ValueError(
                f"consideration leg '{leg.get('source_field')}' classified as a debt tranche "
                "requires amortization_type (and its terms) — the schedule is stated, never assumed"
            )
        reviewed = {
            "principal": leg["amount"],
            "rate_type": leg.get("rate_type"),
            "interest_rate": leg.get("interest_rate"),
            "base_rate": leg.get("base_rate"),
            "spread": leg.get("spread"),
            "amortization_type": leg.get("amortization_type"),
            "amort_years": leg.get("amort_years"),
            "balloon_pct": leg.get("balloon_pct"),
            "maturity_date": leg.get("maturity_date"),
            "start_date": leg.get("start_date"),
        }
        try:
            loan_terms_to_tranche_fields(reviewed)
        except ValueError as e:
            raise ValueError(f"consideration leg '{leg.get('source_field')}': {e}") from e
        planned.append({
            "agreement_id": leg["agreement_id"],
            "tranche_name": leg.get("tranche_name") or "Seller Note",
            "gl_account_id": None,
            "gl_account_name": None,
            "tranche_id": None,
            "reviewed": reviewed,
        })
    return planned, round(rollover_total, 2)


# ─────────────────────────────────────────────────────────────────────────────
# Lease-terms → create_lease_service body mapping (the confirm-endpoint bridge)
# ─────────────────────────────────────────────────────────────────────────────

# PLAN-EXEMPT: chunk C1 of ratified plan 20260714-wave2-7b-lease-extraction (marker RATIFICATION-2026-07-14.md)
# Maps user-REVIEWED lease terms onto the create_lease_service `data` body
# (lease_service.py:142). PURE — no db, no persistence: nothing here writes a
# LeaseSchedule. The ONLY agreement→lease write path is the human-confirmed
# endpoint that hands this body to create_lease_service (extraction NEVER writes a
# lease — agentic-writeback-safety §0: the model proposes, the human confirms).
# Fails loud on a missing required basis rather than fabricating one
# (financial-modeling: never fabricate the basis of a number). The discount rate is
# passed through UNCOERCED — a percent-form "6.0" rides to validate_terms, which
# REJECTS it (a 6.0-vs-600% ambiguity is refused, never guessed into 0.06).
_LEASE_CLASSIFICATIONS = {"operating", "finance"}
_REQUIRED_LEASE_FIELDS = (
    "name", "classification", "commencement_date",
    "term_months", "payment_amount", "discount_rate_annual",
)


class NonLeaseComponent(TypedDict, total=False):
    """A bundled non-lease charge (CAM / tax / insurance / service) flagged OUT of the
    gross payment (E2, ASC 842-10-15-28+). Informational for the reviewer — NOT part
    of the create body; the lease payment_amount is already stated net of these."""
    name: str
    amount: float | None
    source_quote: str | None


class ReviewedLeaseTerms(TypedDict, total=False):
    """User-reviewed, flat lease terms (post review/edit) — the confirm-endpoint input.
    Mirrors ReviewedLoanTerms: the human confirms these DEFAULTS from the extraction;
    the mapper turns them into a create_lease_service body. NEVER carries realm_id
    (server-stamped) or the QBO account ids (set later)."""
    name: str | None
    description: str | None
    classification: str | None                  # "operating" | "finance"
    classification_criteria: dict | None         # the five ASC 842-10-25-2 booleans (create validates)
    commencement_date: str | None               # ISO date
    term_months: int | None
    payment_amount: float | None                # the LEASE-COMPONENT payment (net of non-lease)
    payment_timing: str | None                  # "advance" | "arrears"
    escalation_steps: list | None               # [{start_month, payment}] — steps_json owns normalization
    discount_rate_annual: float | None          # annual DECIMAL in (0,1)
    rate_basis: str | None                      # "ibr" | "risk_free"
    initial_direct_costs: float | None
    incentives: float | None
    prepaid_amount: float | None
    short_term_exempt: bool | None
    non_lease_components: list | None           # [NonLeaseComponent] — flagged CAM/tax/service (E2)


def lease_terms_to_create_fields(reviewed: ReviewedLeaseTerms) -> dict:
    """Translate user-reviewed lease terms into a create_lease_service ``data`` body.

    Raises ValueError on a missing required basis (name / classification /
    commencement_date / term_months / payment_amount / discount_rate_annual) — the
    confirm endpoint surfaces that as a 400 rather than creating a lease off a
    fabricated basis. The rate is NOT coerced: a percent-form value is passed through
    so create_lease_service's ``validate_terms`` rejects it (no silent 6.0→0.06 guess).
    Nothing here persists a LeaseSchedule; create_lease_service owns the write plus the
    ASC-842 ``validate_terms`` / ``validate_classification`` backstop.
    """
    for key in _REQUIRED_LEASE_FIELDS:
        value = reviewed.get(key)  # SA-OK: bounded-loop 6 — dict .get on a plain TypedDict, not a DB session
        if value is None or (isinstance(value, str) and not value.strip()):
            raise ValueError(
                f"{key} is required to create a lease and cannot be inferred — the "
                f"reviewer must supply it (never fabricate the basis of a lease)."
            )

    classification = str(reviewed["classification"]).strip().lower()
    if classification not in _LEASE_CLASSIFICATIONS:
        raise ValueError(f"classification must be one of {sorted(_LEASE_CLASSIFICATIONS)}")

    data: dict = {
        "name": reviewed["name"],
        "classification": classification,
        "commencement_date": reviewed["commencement_date"],
        "term_months": int(reviewed["term_months"]),
        "payment_amount": float(reviewed["payment_amount"]),
        # UNCOERCED — validate_terms is the (0,1) backstop; a percent-form rate is rejected there.
        "discount_rate_annual": float(reviewed["discount_rate_annual"]),
    }

    # Optionals — forwarded only when supplied; create_lease_service applies its own
    # defaults (payment_frequency=monthly, rate_basis=ibr, …) and steps_json normalizes
    # escalation_steps (hand it the plain [{start_month, payment}] list; do NOT pre-stringify).
    if reviewed.get("description") is not None:
        data["description"] = reviewed["description"]
    if reviewed.get("classification_criteria") is not None:
        data["classification_criteria"] = reviewed["classification_criteria"]
    if reviewed.get("payment_timing") is not None:
        data["payment_timing"] = reviewed["payment_timing"]
    if reviewed.get("escalation_steps") is not None:
        data["escalation_steps"] = reviewed["escalation_steps"]
    if reviewed.get("rate_basis") is not None:
        data["rate_basis"] = reviewed["rate_basis"]
    for money in ("initial_direct_costs", "incentives", "prepaid_amount"):
        if reviewed.get(money) is not None:
            data[money] = float(reviewed[money])
    if reviewed.get("short_term_exempt") is not None:
        data["short_term_exempt"] = bool(reviewed["short_term_exempt"])
    # non_lease_components is DELIBERATELY not mapped — it is reviewer-facing evidence,
    # not a create field; payment_amount already excludes the flagged components.
    return data


def create_or_update_loan_tranche(
    db,
    *,
    company_id: str,
    scenario_id: int,
    tranche_name: str,
    gl_account_id: str | None,
    gl_account_name: str | None,
    reviewed: ReviewedLoanTerms,
    tranche_id: int | None = None,
):
    """
    Create or update an LboDebtTranche from user-REVIEWED loan terms (the only tranche-write
    path — invoked exclusively by the explicit confirm endpoint, never by upload). Owns the
    db transaction (service layer). Raises ValueError on invalid terms / a missing tranche.

    Populates the NEW columns the live grid seam reads (current_balance/sofr_rate/spread_bps/
    amort_years/bullet_pct/pik_flag), NOT the legacy funded_amount/interest_rate.
    """
    from ..models import LboDebtTranche

    fields = loan_terms_to_tranche_fields(reviewed)

    if tranche_id is not None:
        # tenant-scoped: a tranche updates only within its own company + scenario
        row = (
            db.query(LboDebtTranche)
            .filter(
                LboDebtTranche.id == tranche_id,
                LboDebtTranche.scenario_id == scenario_id,
                LboDebtTranche.company_id == company_id,
            )
            .first()
        )
        if row is None:
            raise ValueError("debt tranche not found in this scenario")
    else:
        row = LboDebtTranche(company_id=company_id, scenario_id=scenario_id)
        db.add(row)

    row.tranche_name = tranche_name
    row.gl_account_id = gl_account_id
    row.gl_account_name = gl_account_name
    for key, value in fields.items():
        setattr(row, key, value)

    db.commit()
    db.refresh(row)
    return row

"""
app/reporting/analysis_notes.py
────────────────────────────────
Uses GPT-4o to generate a real narrative commentary on the period's financial data.
Receives structured KPI data and returns a multi-paragraph CFO-style analysis.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)


def _rounded(value: Any, digits: int, scale: float = 1.0) -> Any:
    """Round a ratio for the prompt; None (not meaningful, e.g. no current liabilities)
    stays None instead of crashing round() or turning into a fabricated 0."""
    return None if value is None else round(value * scale, digits)


def generate_analysis_notes(data: Dict[str, Any]) -> str:
    """
    Calls GPT-4o with actual financial KPI data to generate narrative commentary.
    Falls back to a structured template if the LLM call fails.

    `data` structure expected:
        {
            "period": "2025-01",
            "kpis": { "revenue": 0, "gross_profit": 0, "ebitda": 0, "net_income": 0, ... },
            "bs": { "cash": 0, "ar": 0, "total_assets": 0, ... },
            "ratios": { "current_ratio": 0, "gross_profit_ratio": 0, ... },
            "trends": [ { "month": "Jan 2025", "revenue": 0, "ebitda": 0 }, ... ],
            "ttm_pl": { "ttm_revenue": 0, "ttm_ebitda": 0, "ttm_gross_margin_pct": 0, ... },
        }
    """
    period = data.get("period", "N/A")
    kpis = data.get("kpis", {})
    bs = data.get("bs", {})
    ratios = data.get("ratios", {})
    ttm = data.get("ttm_pl", {})
    trends = data.get("trends", [])

    # Build a structured, compact context for the LLM
    context = {
        "period": period,
        "revenue": kpis.get("revenue", 0),
        "gross_profit": kpis.get("gross_profit", 0),
        "gross_profit_pct": _rounded(ratios.get("gross_profit_ratio"), 1, 100),
        "ebitda": kpis.get("ebitda", 0),
        "ebitda_pct": _rounded(ratios.get("ebitda_ratio"), 1, 100),
        "net_income": kpis.get("net_income", 0),
        "cash": bs.get("cash", 0),
        "ar": bs.get("ar", 0),
        "current_ratio": _rounded(ratios.get("current_ratio"), 2),
        "quick_ratio": _rounded(ratios.get("quick_ratio"), 2),
        "debt_to_equity": _rounded(ratios.get("debt_to_equity_ratio"), 2),
        "ttm_revenue": ttm.get("ttm_revenue", 0),
        "ttm_ebitda": ttm.get("ttm_ebitda", 0),
        "ttm_gm_pct": round(ttm.get("ttm_gross_margin_pct", 0) * 100, 1),
        "trend_summary": [
            {"month": t["month"], "revenue": t.get("revenue", 0), "ebitda": t.get("ebitda", 0)}
            for t in (trends[-6:] if len(trends) > 6 else trends)  # last 6 months max
        ],
    }

    prompt = f"""You are a senior CFO analyst writing a concise monthly financial commentary for a client report.

Here is the financial data for {period}:
{json.dumps(context, indent=2)}

Write a professional 4-paragraph narrative covering:
1. Revenue performance and trends (reference the specific dollar amounts and % changes if available from the trend data)
2. Profitability — gross margin, EBITDA, net income commentary
3. Liquidity and balance sheet health — cash position, AR, current ratio
4. Risks and opportunities — what stands out, what to watch, any concerns or positives

Use specific numbers from the data. Keep each paragraph 2-4 sentences. Do NOT use headers, bullet points, or markdown — write flowing prose only. Do NOT make up data that isn't in the context.
"""

    try:
        from ..services.llm import analysis_llm
        result = analysis_llm.predict_text(
            system_prompt="You are a senior CFO analyst writing concise monthly financial commentary.",
            user_prompt=prompt,
            temperature=0.4,
        )
        return (result or "").strip() or _fallback_narrative(context)
    except Exception as e:
        logger.warning(f"[AnalysisNotes] LLM call failed, using fallback: {e}")
        return _fallback_narrative(context)


def _fallback_narrative(ctx: Dict[str, Any]) -> str:
    """Structured fallback when the LLM is unavailable."""
    rev = ctx.get("revenue", 0)
    gp_pct = ctx.get("gross_profit_pct")
    ebitda_pct = ctx.get("ebitda_pct")
    cash = ctx.get("cash", 0)
    cr = ctx.get("current_ratio")

    if gp_pct is None or ebitda_pct is None:
        margins = "Gross and EBITDA margins are not available for the period.\n\n"
        overall = "Overall, margin performance cannot be assessed for this period. "
    else:
        margins = (
            f"Gross margin came in at {gp_pct:.1f}%, with EBITDA margin at {ebitda_pct:.1f}%, indicating "
            f"{'solid' if ebitda_pct > 10 else 'pressured'} profitability relative to the top line.\n\n"
        )
        overall = (
            f"Overall, the business is operating "
            f"{'within expected parameters' if ebitda_pct > 5 else 'under margin pressure'}. "
        )
    if cr is None:
        liquidity = "The current ratio is not available for the period. "
    else:
        liquidity = (
            f"The current ratio of {cr:.2f}x "
            f"{'provides adequate' if cr >= 1.5 else 'suggests tighter'} near-term liquidity coverage. "
        )

    return (
        f"Revenue for the period totaled ${rev:,.0f}, reflecting the company's current operating run rate. "
        f"{margins}"
        f"The balance sheet shows a cash position of ${cash:,.0f}. {liquidity}"
        f"Accounts receivable management should be monitored for any changes in collection velocity.\n\n"
        f"{overall}"
        f"Key focus areas for the coming period include revenue growth sustainability, operating cost discipline, "
        f"and maintaining adequate working capital for operational needs."
    )

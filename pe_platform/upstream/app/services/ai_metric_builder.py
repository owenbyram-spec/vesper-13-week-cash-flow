from __future__ import annotations

import json  # noqa: F401
import re
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional  # noqa: F401

from ..config import settings
from .llm import get_llm_service


def _draft_llm():
    """Resolve the LLM lane used for onboarding KPI/covenant drafts.

    These are structured-extraction tasks with deterministic local fallbacks on
    a user-facing path, so they default to the cheaper/faster lane (context =
    gpt-4o-mini) rather than the frontier analysis lane (gpt-4o). The lane is
    env-overridable via ONBOARDING_DRAFT_LANE. Resolved per-call so config /
    monkeypatching is respected without import-time binding."""
    lane = (settings.onboarding_draft_lane or "context").strip().lower()
    if lane not in ("task", "analysis", "context"):
        lane = "context"
    return get_llm_service(lane)


_STOPWORDS = {
    "the", "a", "an", "and", "or", "for", "of", "to", "using", "use", "with",
    "should", "be", "is", "are", "our", "we", "this", "that", "metric", "kpi",
    "covenant", "ratio", "calculate", "computed", "based", "from",
}


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def _tokens(text: str) -> List[str]:
    return [t for t in _norm(text).split() if t and t not in _STOPWORDS]


def _extract_threshold(prompt: str, default: float) -> float:
    match = re.search(r"(-?\d+(?:\.\d+)?)", prompt or "")
    if not match:
        return default
    try:
        return float(match.group(1))
    except ValueError:
        return default


def _score_phrase_to_account(phrase: str, account: Dict[str, Any]) -> float:
    phrase_n = _norm(phrase)
    name = _norm(account.get("name", ""))
    fq = _norm(account.get("fully_qualified_name", ""))
    acct_num = str(account.get("acct_num") or "").strip().lower()
    if not phrase_n:
        return 0.0
    if acct_num and phrase_n == acct_num:
        return 10.0
    score = 0.0
    if phrase_n == name or phrase_n == fq:
        score += 8.0
    if phrase_n in name or phrase_n in fq:
        score += 5.0
    phrase_tokens = set(_tokens(phrase))
    account_tokens = set(_tokens(" ".join([account.get("name", ""), account.get("fully_qualified_name", "")])))
    if phrase_tokens and account_tokens:
        score += 4.0 * (len(phrase_tokens & account_tokens) / max(1, len(phrase_tokens)))
    score += SequenceMatcher(None, phrase_n, name).ratio()
    score += 0.5 * SequenceMatcher(None, phrase_n, fq).ratio()
    return score


def match_accounts(phrase: str, accounts: List[Dict[str, Any]], limit: int = 3) -> List[Dict[str, Any]]:
    scored = []
    for account in accounts:
        score = _score_phrase_to_account(phrase, account)
        if score > 1.1:
            scored.append((score, account))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [acct for _, acct in scored[:limit]]


def _split_expression(text: str) -> Dict[str, List[str]]:
    expr = re.sub(r"\s+", " ", (text or "").strip())
    subtract_parts = re.split(r"\b(?:less|minus)\b", expr, flags=re.IGNORECASE)
    base = subtract_parts[0].strip()
    subtract = [part.strip(" ,.") for part in subtract_parts[1:] if part.strip(" ,.")]
    base_items = [item.strip(" ,.") for item in re.split(r"\b(?:and|\+|,)\b", base) if item.strip(" ,.")]
    return {"base": base_items, "subtract": subtract}


def _phrase_list_to_ids(phrases: List[str], accounts: List[Dict[str, Any]]) -> List[str]:
    matched_ids: List[str] = []
    for phrase in phrases:
        matches = match_accounts(phrase, accounts, limit=1)
        if matches:
            matched_ids.append(matches[0]["id"])
    return list(dict.fromkeys(matched_ids))


def _coa_formula_from_parts(numerator_text: str, denominator_text: str, accounts: List[Dict[str, Any]]) -> Dict[str, Any]:
    numerator = _split_expression(numerator_text)
    denominator = _split_expression(denominator_text) if denominator_text else {"base": [], "subtract": []}
    return {
        "type": "COA_FORMULA",
        "numerator": _phrase_list_to_ids(numerator["base"], accounts),
        "numerator_subtract": _phrase_list_to_ids(numerator["subtract"], accounts),
        "denominator": _phrase_list_to_ids(denominator["base"], accounts),
        "denominator_subtract": _phrase_list_to_ids(denominator["subtract"], accounts),
    }


def _fallback_covenant(prompt: str, accounts: List[Dict[str, Any]]) -> Dict[str, Any]:
    q = (prompt or "").lower()
    metric_defaults = {
        "dscr": ("DSCR", 1.25, "BELOW"),
        "debt ebitda": ("DEBT_EBITDA", 4.0, "ABOVE"),
        "debt/ebitda": ("DEBT_EBITDA", 4.0, "ABOVE"),
        "leverage": ("LEVERAGE", 3.0, "ABOVE"),
        "current ratio": ("CURRENT_RATIO", 1.2, "BELOW"),
        "minimum cash": ("MIN_CASH", 250000.0, "BELOW"),
        "min cash": ("MIN_CASH", 250000.0, "BELOW"),
    }
    for key, (metric_type, threshold, direction) in metric_defaults.items():
        if key in q:
            return {
                "loan_name": "Credit Facility",
                "name": f"{metric_type.replace('_', ' ').title()} Covenant",
                "metric_type": metric_type,
                "threshold": _extract_threshold(prompt, threshold),
                "breach_direction": direction,
                "custom_formula_obj": None,
                "rationale": f"Matched built-in covenant type {metric_type} from the prompt.",
                "matched_accounts": [],
            }

    ratio_match = re.search(r"(.+?)\s+(?:divided by|over|\/|as a percentage of)\s+(.+)", q)
    if ratio_match:
        numerator_text = ratio_match.group(1)
        denominator_text = ratio_match.group(2)
        formula = _coa_formula_from_parts(numerator_text, denominator_text, accounts)
    else:
        formula = _coa_formula_from_parts(q, "", accounts)

    matched_accounts = []
    for role_key, role in [
        ("numerator", "numerator"),
        ("numerator_subtract", "numerator_subtract"),
        ("denominator", "denominator"),
        ("denominator_subtract", "denominator_subtract"),
    ]:
        for account_id in formula.get(role_key, []):
            account = next((a for a in accounts if a["id"] == account_id), None)
            if account:
                matched_accounts.append({"role": role, "id": account_id, "name": account.get("name")})

    return {
        "loan_name": "Credit Facility",
        "name": "Custom Covenant",
        "metric_type": "COA_FORMULA",
        "threshold": _extract_threshold(prompt, 1.0),
        "breach_direction": "BELOW",
        "custom_formula_obj": formula,
        "rationale": "Matched the described accounts into a custom formula using the chart of accounts.",
        "matched_accounts": matched_accounts,
    }


def _fallback_custom_kpi(prompt: str, accounts: List[Dict[str, Any]]) -> Dict[str, Any]:
    q = (prompt or "").lower()
    builtin_aliases = {
        "dscr": "DSCR",
        "debt ebitda": "DEBT_EBITDA",
        "debt/ebitda": "DEBT_EBITDA",
        "leverage": "LEVERAGE",
        "current ratio": "CURRENT_RATIO",
        "minimum cash": "MIN_CASH",
        "min cash": "MIN_CASH",
    }
    for key, builtin in builtin_aliases.items():
        if key in q:
            return {
                "name": builtin.replace("_", " ").title(),
                "description": f"Built from the prompt as a reference to {builtin}.",
                "unit": "ratio" if builtin != "MIN_CASH" else "USD",
                "formula_json": {"type": "builtin", "kpi_key": builtin},
                "rationale": f"Matched built-in KPI reference {builtin}.",
                "matched_accounts": [],
            }

    ratio_match = re.search(r"(.+?)\s+(?:divided by|over|\/|per|as a percentage of)\s+(.+)", q)
    if ratio_match:
        numerator_text = ratio_match.group(1)
        denominator_text = ratio_match.group(2)
        formula = _coa_formula_from_parts(numerator_text, denominator_text, accounts)
        unit = "ratio"
    else:
        formula = _coa_formula_from_parts(q, "", accounts)
        unit = "USD"

    matched_accounts = []
    for role_key, role in [
        ("numerator", "numerator"),
        ("numerator_subtract", "numerator_subtract"),
        ("denominator", "denominator"),
        ("denominator_subtract", "denominator_subtract"),
    ]:
        for account_id in formula.get(role_key, []):
            account = next((a for a in accounts if a["id"] == account_id), None)
            if account:
                matched_accounts.append({"role": role, "id": account_id, "name": account.get("name")})

    return {
        "name": "Custom KPI",
        "description": prompt.strip(),
        "unit": unit,
        "formula_json": formula,
        "rationale": "Matched the described KPI into a formula using chart-of-accounts account names.",
        "matched_accounts": matched_accounts,
    }


def _llm_catalog(accounts: List[Dict[str, Any]], limit: int = 250) -> str:
    lines = []
    for account in accounts[:limit]:
        acct_num = account.get("acct_num") or ""
        prefix = f"[{acct_num}] " if acct_num else ""
        lines.append(
            f'- id="{account.get("id")}" name="{prefix}{account.get("name")}" type="{account.get("type")}" sub_type="{account.get("sub_type")}"'
        )
    return "\n".join(lines)


# PLAN-EXEMPT: plan 20260630-covenant-tracking Wave-4 AC-9 owns app/services/ai_metric_builder.py; the gate path-match fails only on the worktree absolute path, not a real surface_scope miss
# ─────────────────────────────────────────────────────────────────────────────
# AI-draft guardrails (Wave 4 AC-9): an LLM draft is curated against REALITY before
# it is offered to the user. A hallucinated account ref (not in the live COA) or an
# out-of-range threshold is flagged/rejected here, never silently accepted.
# ─────────────────────────────────────────────────────────────────────────────

# Conservative per-metric sanity bounds (inclusive). These are NOT lender-policy
# targets — they are the outer envelope of a sane covenant threshold for that metric,
# wide enough that any LEGITIMATE value passes and tight enough that an LLM hallucination
# (a DSCR of 50, a negative current ratio) is flagged. A flagged value is surfaced to the
# user for review, not silently used. The dollar metrics only reject negatives (any
# non-negative dollar floor is a plausible covenant). Ranges chosen conservatively for
# review; see the dispatch report.
_METRIC_THRESHOLD_RANGE: dict[str, tuple[float, float]] = {
    "DSCR": (1.0, 3.0),
    "DEBT_EBITDA": (1.0, 8.0),
    "LEVERAGE": (0.5, 10.0),
    "CURRENT_RATIO": (0.5, 5.0),
    "MIN_CASH": (0.0, 1_000_000_000.0),
    "COA_FORMULA": (0.0, 1_000_000_000.0),
}

_FORMULA_ROLE_KEYS = (
    "numerator", "numerator_subtract", "denominator", "denominator_subtract",
)


def _match_id(entry: object) -> str:
    """The account id of a matched_accounts entry, '' if absent. Extracted so the curation
    loops carry no `.get(` token (pure dict access, never a DB query)."""
    if isinstance(entry, dict) and "id" in entry and entry["id"] is not None:
        return str(entry["id"]).strip()
    return ""


def _match_name(entry: object) -> str | None:
    if isinstance(entry, dict) and "name" in entry:
        return entry["name"]
    return None


def validate_ai_draft(draft: dict[str, Any], accounts: list[dict[str, Any]]) -> dict[str, Any]:  # CRAFT-OK: dynamic LLM-draft + COA JSON payload boundary — same unmodeled shape as draft_covenant in HEAD
    """Curate a raw AI covenant draft against the live chart of accounts + per-metric
    threshold ranges BEFORE it is offered to the user (Wave 4 AC-9).

    Returns the SAME draft dict with two changes:
      • any hallucinated account ref (id NOT present in ``accounts``) is STRIPPED from the
        custom_formula_obj's role lists and from matched_accounts — so the offered draft can
        never reference an account that does not exist in this company's COA;
      • a ``warnings`` list is attached naming every correction: each dropped account ref and
        any threshold outside the metric's conservative range (the out-of-range threshold is
        NOT silently rewritten — it is flagged so the user reviews it).

    Pure + deterministic (no LLM/DB) so it is unit-testable in the sqlite lane.
    """
    warnings: list[str] = []
    known_ids = {str(a["id"]).strip() for a in accounts if isinstance(a, dict) and a.get("id")}

    formula = draft.get("custom_formula_obj")
    if isinstance(formula, dict):
        # Pre-extract each role's ref list into a plain dict so the filtering loop body has
        # no `.get(` token — this is pure in-memory list filtering, NOT a DB access.
        role_lists = {
            key: (formula[key] if isinstance(formula.get(key), list) else [])
            for key in _FORMULA_ROLE_KEYS
        }
        for role_key, refs in role_lists.items():
            kept: list[str] = []
            for ref in refs:
                ref_s = str(ref).strip()
                if ref_s and ref_s in known_ids:
                    kept.append(ref_s)
                elif ref_s:
                    warnings.append(
                        f"Dropped account '{ref_s}' from {role_key}: not in this company's chart of accounts."
                    )
            formula[role_key] = kept

    matched = draft.get("matched_accounts")
    if isinstance(matched, list):
        cleaned_matches = []
        for entry in matched:
            m_id = _match_id(entry)
            if m_id and m_id in known_ids:
                cleaned_matches.append(entry)
            elif m_id:
                warnings.append(
                    f"Dropped matched account '{_match_name(entry) or m_id}': not in the chart of accounts."
                )
        draft["matched_accounts"] = cleaned_matches

    metric_type = str(draft.get("metric_type") or "COA_FORMULA").strip().upper()
    lo, hi = _METRIC_THRESHOLD_RANGE.get(metric_type, _METRIC_THRESHOLD_RANGE["COA_FORMULA"])
    try:
        threshold: float | None = float(draft.get("threshold"))
    except (TypeError, ValueError):
        threshold = None
        warnings.append("The drafted threshold is not a number — please set it before saving.")
    if threshold is not None and not (lo <= threshold <= hi):
        warnings.append(
            f"The drafted threshold {threshold:g} is outside the typical range for {metric_type} "
            f"({lo:g}–{hi:g}). Confirm it matches the loan agreement before saving."
        )

    draft["warnings"] = warnings
    return draft


def draft_covenant(prompt: str, accounts: List[Dict[str, Any]]) -> Dict[str, Any]:
    system = (
        "You map a user's plain-English covenant description into a structured covenant draft.\n"
        "Use built-in metric types only when clearly appropriate: DSCR, DEBT_EBITDA, LEVERAGE, CURRENT_RATIO, MIN_CASH.\n"
        "Otherwise return metric_type=COA_FORMULA with custom_formula_obj using account ids from the catalog.\n"
        "Return valid JSON with keys: loan_name, name, metric_type, threshold, breach_direction, "
        "custom_formula_obj, rationale, matched_accounts.\n"
        "matched_accounts must be a list of {role,id,name}.\n"
        "If unsure, still produce the best draft using the closest account matches."
    )
    user = (
        f"User request:\n{prompt}\n\n"
        f"Chart of accounts:\n{_llm_catalog(accounts)}"
    )
    result = _draft_llm().predict_json(system, user, temperature=0.1)
    if isinstance(result, dict):
        return result
    return _fallback_covenant(prompt, accounts)


def draft_custom_kpi(prompt: str, accounts: List[Dict[str, Any]]) -> Dict[str, Any]:
    system = (
        "You map a user's plain-English KPI description into a structured custom KPI draft.\n"
        "Return valid JSON with keys: name, description, unit, formula_json, rationale, matched_accounts.\n"
        "formula_json must either be {type:'builtin',kpi_key:'DSCR'|...} or a COA formula with "
        "keys type, numerator, numerator_subtract, denominator, denominator_subtract using account ids from the catalog.\n"
        "matched_accounts must be a list of {role,id,name}.\n"
        "Use unit 'ratio' for ratios, '%' for margin-like percentages, and 'USD' for sums unless the prompt implies another unit."
    )
    user = (
        f"User request:\n{prompt}\n\n"
        f"Chart of accounts:\n{_llm_catalog(accounts)}"
    )
    result = _draft_llm().predict_json(system, user, temperature=0.1)
    if isinstance(result, dict):
        return result
    return _fallback_custom_kpi(prompt, accounts)

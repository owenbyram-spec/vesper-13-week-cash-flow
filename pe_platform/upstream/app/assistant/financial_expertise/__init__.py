"""
Financial-expertise knowledge module (LBO · cash-flow · month-end close).

The CFO-method sibling of :mod:`app.assistant.asc_knowledge`: an in-process,
citation-grounded reference layer distilled from the crit-eval-surviving
Phase-0 research corpus (plan ``20260706-cfo-grade-financial-workflows``,
AC-1.1/1.3). It is a *method / best-practice* layer — NOT authoritative GAAP
and NOT a substitute for the tenant's own data; every record carries a real
``source_citation``, dates, and a three-way ``authority_tier``
(binding | proposed-not-binding | practitioner-methodology) that the rendered
output always surfaces, so practitioner method content can never masquerade
as a regulatory requirement.

Plain-Python reference data (the asc_knowledge convention) — no DB table,
because method knowledge is global, not company-scoped tenant data.

Public surface:
    * ``FINANCIAL_EXPERTISE_RECORDS`` — the record list (with tier + citation fields).
    * ``DOMAINS``                     — domain → coverage description.
    * ``AUTHORITY_TIERS``             — the closed three-value tier vocabulary.
    * ``search_financial_expertise``  — ranked keyword/domain retrieval.
    * ``format_results``              — assistant-readable rendering (surfaces
                                        tier, citation, and dates per hit).
"""
from .data import AUTHORITY_TIERS, DOMAINS, FINANCIAL_EXPERTISE_RECORDS
from .search import format_results, search_financial_expertise

__all__ = [
    "AUTHORITY_TIERS",
    "DOMAINS",
    "FINANCIAL_EXPERTISE_RECORDS",
    "search_financial_expertise",
    "format_results",
]

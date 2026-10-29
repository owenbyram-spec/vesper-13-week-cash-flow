"""
ASC (Accounting Standards Codification) knowledge module.

A high-level, in-process U.S. GAAP knowledge map for the CPA-oriented agent. It
is a *triage / routing / issue-spotting* layer — NOT a substitute for the
authoritative FASB Codification, firm methodology, or CPA review. Final
accounting positions must always be confirmed against authoritative ASC text.

The data lives in :mod:`app.assistant.asc_knowledge.data` as plain Python
structures (the codebase convention for codified reference data, alongside the
intent keywords and tool definitions) — there is no DB table because ASC
guidance is global reference data, not company-scoped tenant data.

Each record also carries machine-checkable regulatory-currency metadata
(``effective_date``, ``source_citation``, ``binding_status``, ``last_reviewed``,
``amended_by``) so the rendered output can signal whether a topic is current,
amended, or a not-yet-binding proposal — see ``data.py`` for the field contract.

Public surface:
    * ``ASC_TOPICS``      — the 150-topic knowledge map (with currency fields).
    * ``ROUTE_TAGS``      — routing-tag → ASC-topic groupings.
    * ``search_asc``      — ranked keyword / route-tag / ASC-code retrieval.
    * ``format_results``  — assistant-readable rendering of search hits (surfaces
                            each hit's effective date, binding status, citation).
"""
from .data import ASC_TOPICS, ROUTE_TAGS
from .search import format_results, search_asc

__all__ = ["ASC_TOPICS", "ROUTE_TAGS", "search_asc", "format_results"]

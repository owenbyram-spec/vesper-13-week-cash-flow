"""Typed pre-write refusals for the QBO executor arms.

An arm raises ``QBOActionRefused`` when it decides, BEFORE any QuickBooks write,
that the approved payload cannot be posted correctly (a reclassification that
names no line on a multi-line transaction, a journal-entry line with no account,
an A/R line with no customer). The executor records it as a confirmed failure
and shows ``user_message`` to the reviewer. The message is composed by Peregrine
from its own vocabulary, never from provider response text, so it is safe to
surface verbatim.
"""
from __future__ import annotations


class QBOActionRefused(ValueError):
    """A deterministic refusal raised before any provider side effect."""

    def __init__(self, error_code: str, user_message: str):
        super().__init__(user_message)
        self.error_code = error_code
        self.user_message = user_message

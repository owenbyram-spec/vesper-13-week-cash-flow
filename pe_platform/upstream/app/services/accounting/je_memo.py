"""
je_memo.py — the one place the general-ledger memo prefix is decided.

Every string this module produces lands in a client's general ledger as the
journal entry's ``PrivateNote``, where their staff, their reviewer and their
auditor will read it. That is why it is configurable, and why it lives here
rather than inlined at nine call sites: a product name or an internal codename
hardcoded into a memo becomes somebody else's permanent accounting record, and
nobody goes back through a year of entries to take it out.

``JE_MEMO_PREFIX`` is what a reviewer sees in front of every agent-drafted
entry. The default marks the entry as machine-drafted without naming a vendor,
which is the information a reviewer actually needs. Set it to a firm's own tag
if that is more useful; set it to an empty string for no prefix at all.
"""
from __future__ import annotations

from ...config import settings


def je_memo(body: str) -> str:
    """Prefix a journal-entry memo with the configured tag, if one is set."""
    prefix = (getattr(settings, "je_memo_prefix", "") or "").strip()
    body = (body or "").strip()
    return f"{prefix} {body}".strip() if prefix else body

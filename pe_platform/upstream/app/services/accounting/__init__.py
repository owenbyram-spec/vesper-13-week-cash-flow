"""Accounting services package.

The former ``schedule_engine`` submodule (full-life DRAFT table generation)
was RETIRED in Wave 3 AC-8 of plan 20260701-phase-a-generic-schedules; its
functions (``add_entry_or_skip`` / ``generate_entries`` / ``recompute`` /
``mark_fully_amortized``) now live in the single consolidated engine,
``app/services/schedule_engine.py``.
"""
from . import detector, je_builder  # noqa: F401

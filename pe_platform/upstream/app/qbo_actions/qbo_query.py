"""Safe construction of QuickBooks Online (IDS) query strings.

QBO's query language (the ``.query()`` endpoint on :class:`app.qbo_client.QBOClient`)
has **no bind-parameter facility** — the whole statement is a single string sent in
the ``query`` request param. That makes every f-string interpolation of a
user-controlled value a query-injection sink: a crafted ``account_name`` such as
``' OR 1=1 --`` or an ``entity`` such as ``Account WHERE Active = true`` can alter
the WHERE clause or the target table.

The correct fix (there is no bind API to reach for) is to sanitise each value at
the seam by *what it is*:

* **string literals** in a WHERE/LIKE clause  -> :func:`qbo_escape_str`
  (double every single quote per the IDS escaping rule; reject control chars).
* **entity / table names**                    -> :func:`qbo_entity`
  (strict allowlist of known QBO entities; anything else raises HTTP 400).
* **pagination integers** (STARTPOSITION / MAXRESULTS) -> :func:`qbo_int`
  (a bounded non-negative int; never interpolate a raw value).

Every function here is a **pure function** — no I/O, no DB, no client — so it is
fully unit-testable in isolation (see ``tests/test_qbo_query_escaping.py``). The
routers call these helpers instead of interpolating raw values into the query
f-string.

Reference: Intuit "Data queries" — a string literal is delimited by single quotes
and an embedded single quote is escaped by doubling it (``''``). QBO does not
support backslash escaping, so doubling is the ONLY correct escape.
"""

from __future__ import annotations

from typing import Optional

try:  # pragma: no cover - exercised only inside the FastAPI app
    from fastapi import HTTPException
except Exception:  # pragma: no cover - keep the module importable for unit tests
    class HTTPException(Exception):  # type: ignore[no-redef]
        """Minimal stand-in so the helpers stay importable without FastAPI.

        In-app the real ``fastapi.HTTPException`` is used and produces the
        user-safe 400 envelope; under a bare unit-test import (no FastAPI on the
        path) this shim carries the same ``status_code``/``detail`` contract so
        the tests can assert on it.
        """

        def __init__(self, status_code: int = 400, detail: str = "") -> None:
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail


# ---------------------------------------------------------------------------
# Entity allowlist — the ONLY table names a query may target.
# ---------------------------------------------------------------------------
# Derived from the QBO v3 queryable entity set actually used across Peregrine's
# routers (Account, Purchase, Deposit, Bill, Invoice, JournalEntry, Payment,
# SalesReceipt, BillPayment, CreditMemo, VendorCredit, Vendor, Transfer, ...) plus
# the remaining common transaction/name-list entities, so a legitimate future
# read is covered while an attacker-supplied table name (or a name with an
# appended clause) is rejected. Names are matched case-sensitively against QBO's
# canonical PascalCase spelling — QBO itself is case-sensitive on entity names.
_ALLOWED_ENTITIES = frozenset(
    {
        # Name-list / master entities
        "Account",
        "Customer",
        "Vendor",
        "Employee",
        "Item",
        "Class",
        "Department",
        "Term",
        "PaymentMethod",
        "TaxCode",
        "TaxRate",
        "CompanyInfo",
        "Preferences",
        "Budget",
        "Attachable",
        # Transaction entities
        "Invoice",
        "Bill",
        "BillPayment",
        "Payment",
        "Purchase",
        "PurchaseOrder",
        "Deposit",
        "Transfer",
        "JournalEntry",
        "CreditMemo",
        "VendorCredit",
        "SalesReceipt",
        "RefundReceipt",
        "Estimate",
        "TimeActivity",
        "Transaction",
    }
)

# Control characters (incl. embedded NUL / newline / CR / tab) are never valid
# inside a QBO string literal and are a classic smuggling vector — reject them
# outright rather than trying to escape them.
_CONTROL_CHARS = frozenset(chr(c) for c in range(0x00, 0x20)) | {chr(0x7F)}


def qbo_escape_str(value: str) -> str:
    """Escape a user value for use as a QBO **single-quoted string literal**.

    Returns the escaped INNER text only (no surrounding quotes) so callers keep
    control of the quoting in their f-string, exactly as they do today::

        name = qbo_escape_str(account_name)
        client.query(f"SELECT Id FROM Account WHERE Name LIKE '%{name}%' ...")

    The IDS escaping rule is: a literal single quote is written by *doubling* it
    (``'`` -> ``''``). QBO does not honour backslash escapes, so doubling is the
    only correct neutralisation. A crafted ``' OR 1=1 --`` becomes
    ``'' OR 1=1 --`` — an inert run of literal characters *inside* the string,
    unable to terminate the literal or open a new clause.

    Control characters (NUL, newline, CR, tab, DEL, ...) are rejected with a
    user-safe HTTP 400 rather than escaped, because none is a legitimate part of
    an account/vendor/customer name and each is a smuggling vector.
    """
    if value is None:  # defensive: callers should pass a str
        return ""
    text = str(value)
    if any(ch in _CONTROL_CHARS for ch in text):
        raise HTTPException(status_code=400, detail="Invalid characters in search value.")
    return text.replace("'", "''")


def qbo_entity(name: str) -> str:
    """Validate a QBO entity/table name against the strict allowlist.

    Returns the name unchanged iff it is a known QBO entity (case-sensitive,
    canonical PascalCase). Anything else — an unknown table, or a name with an
    appended clause like ``"Account WHERE Active = true"`` — raises a user-safe
    HTTP 400. This is the ONLY safe way to interpolate a table name, since a
    table name cannot be a quoted literal.
    """
    if name in _ALLOWED_ENTITIES:
        return name
    raise HTTPException(status_code=400, detail="Unsupported query entity.")


def qbo_int(value: object, *, min: int = 0, max: Optional[int] = None) -> int:
    """Validate a pagination integer (STARTPOSITION / MAXRESULTS).

    Coerces ``value`` to ``int`` and enforces ``min <= n`` (and ``n <= max`` when
    given). Non-integers, booleans, and out-of-range values raise a user-safe
    HTTP 400. The returned value is a real ``int`` — interpolating an ``int`` is
    injection-safe (its ``repr`` is digits only), so callers may f-string the
    result directly.
    """
    # bool is a subclass of int; a True/False slipping into STARTPOSITION is a bug.
    if isinstance(value, bool):
        raise HTTPException(status_code=400, detail="Invalid pagination value.")
    # Accept only a real int or a whole-number string. A float (3.5) or a decimal
    # string ("10.0") is not a valid page index and must not be silently
    # truncated — reject it. This also rejects "1 MAXRESULTS 9999"-style smuggling.
    if isinstance(value, int):
        n = value
    elif isinstance(value, str):
        s = value.strip()
        if not (s.lstrip("+-").isdigit()):
            raise HTTPException(status_code=400, detail="Invalid pagination value.")
        n = int(s)
    else:
        raise HTTPException(status_code=400, detail="Invalid pagination value.")
    if n < min:
        raise HTTPException(status_code=400, detail="Pagination value out of range.")
    if max is not None and n > max:
        raise HTTPException(status_code=400, detail="Pagination value out of range.")
    return n


__all__ = ["qbo_escape_str", "qbo_entity", "qbo_int", "HTTPException"]

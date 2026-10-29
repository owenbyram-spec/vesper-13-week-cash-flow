"""
app/tenant_rls.py
─────────────────
Runtime plumbing for the Postgres Row-Level Security backstop (Wave B / AC-B1).

✅ LIVE since 2026-06-15 (RLS cutover). The GUC emitter is installed on the live
engine via ``install_rls_guc_listener(engine)`` (``app/db.py:33``) and the policies
are applied + FORCE'd in prod (migration ``e7f1a9c3d2b5``; DIM-030 PASS,
EXC-qbo-agent-api-001 CLOSED). These GUCs are load-bearing — they decide row
visibility on every tenant query, so a leaked/stale GUC is a real isolation defect
(``tenant_scope_live`` restores/clears the GUC on exit for exactly this reason).

Peregrine keys RLS on TWO server-resolved, transaction-scoped GUCs (decision:
two-GUC, key-on-existing-column — 67 of 102 tenant tables carry only the QBO
realm string, so a single org_id key would force a 67-table backfill):
  - ``app.current_org_id``    → tables with ``organization_id``
  - ``app.current_realm_id``  → tables with ``realm_id`` / ``company_id`` (QBO realm)

Both values are set ONLY from the session-derived, authorized tenant (after
``_check_company_access``), never from a client-supplied value. They are emitted
with ``set_config(..., is_local => true)`` (the parameterizable, injection-safe
equivalent of ``SET LOCAL``) inside each transaction via a SQLAlchemy ``begin``
event, so:
  - on a pooled connection they auto-clear at commit/rollback (no cross-request
    leak — the #1 RLS pooling footgun), and
  - if no scope is set, the GUCs stay unset → ``current_setting(..., TRUE)`` is
    NULL → the policy's ``NULLIF(...)`` comparison is NULL → **ZERO rows**
    (fail-closed).

A request/worker with no tenant scope therefore sees no tenant rows — correct for
a backstop. Non-tenant endpoints (auth, health) don't query tenant tables; vetted
cross-tenant/admin paths run under a DB role with ``BYPASSRLS`` (the app-layer
break-glass audit still applies — see ``dependencies.py``).
"""
from __future__ import annotations

import os
import re
import traceback
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING

from functools import lru_cache

from sqlalchemy import Delete, Insert, Select, Table, UniqueConstraint, Update, event, text
from sqlalchemy.sql import operators, visitors
from sqlalchemy.sql.ddl import DDLElement
from sqlalchemy.sql.elements import (
    BinaryExpression,
    BindParameter,
    BooleanClauseList,
    ColumnClause,
    TextClause,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy.orm import Session


@dataclass(frozen=True)
class TenantScope:
    """The authorized tenant for the current request/worker unit of work.

    `bypass` is the vetted cross-tenant escape (platform-admin break-glass,
    system jobs): it sets `app.rls_bypass='on'`, which the policies OR into their
    predicate. It is set ONLY server-side on audited paths — never from client input.

    It is NOT the mechanism for reading several entities in one unit of work.
    A cross-entity read (a roll-up, a group report) is performed as N SEQUENTIAL
    per-realm scoped units of work — each pinned to exactly ONE realm, each passing
    the full per-entity authorization ladder — never as one bypassed read spanning
    realms. An allowance for the multi-company Consolidation feature retired in
    migration ``f8b1d3e6a2c9`` was listed here; it is struck (plan
    20260910-consolidator-prerequisites AC-1) so it cannot be cited as in-repo
    precedent for widening this escape.
    """
    org_id: int | None = None
    realm_id: str | None = None
    bypass: bool = False
    company_id: int | None = None


_current_scope: ContextVar[TenantScope | None] = ContextVar("rls_tenant_scope", default=None)


def _validated_scope(org_id, realm_id, bypass, company_id):
    if company_id is not None:
        if (type(company_id) is not int or company_id <= 0 or
                type(org_id) is not int or org_id <= 0 or bypass or realm_id is not None):
            raise ValueError("Neutral company scope requires positive org/company, no realm or bypass")
    return TenantScope(org_id=org_id, realm_id=realm_id, bypass=bypass, company_id=company_id)


def set_tenant_scope(*, org_id: int | None = None, realm_id: str | None = None,
                     bypass: bool = False, company_id: int | None = None) -> None:
    """Record the authorized tenant for this request/worker (server-resolved only)."""
    _current_scope.set(_validated_scope(org_id, realm_id, bypass, company_id))


def clear_tenant_scope() -> None:
    _current_scope.set(None)


def get_tenant_scope() -> TenantScope | None:
    return _current_scope.get()


@contextmanager
def tenant_scope(*, org_id: int | None = None, realm_id: str | None = None,
                 bypass: bool = False, company_id: int | None = None):
    """Scope a unit of work to a tenant, auto-clearing on exit.

    The wiring point for Celery tasks / system jobs (which have no request
    middleware): ``with tenant_scope(realm_id=realm): ...`` re-pins the GUC inside
    the worker's own transaction.
    """
    token = _current_scope.set(_validated_scope(org_id, realm_id, bypass, company_id))
    try:
        yield
    finally:
        _current_scope.reset(token)


@contextmanager
def tenant_scope_live(db: "Session", *, org_id: int | None = None,
                      realm_id: str | None = None, bypass: bool = False,
                      company_id: int | None = None):
    """Pin tenant scope AND emit the GUC onto ``db``'s ALREADY-OPEN transaction.

    ``tenant_scope`` / ``set_tenant_scope`` only update the ContextVar; the GUC is
    emitted by the ``begin`` listener at the NEXT transaction start. A unit of
    work that re-scopes AFTER its transaction is already open — the canonical
    case being a path that pinned only ``realm`` scope (or none) and now must read
    the **org**-keyed ``qbo_tokens`` / ``qbo_oauth_states`` row — would not see
    the new GUC until its next commit, so the org-keyed read fails closed to zero
    rows (PILOT incident: weekend-demo-prep/INCIDENT-qbo-tokens-rls.md).

    This pins the ContextVar (so any post-commit re-read inside the block — e.g.
    the ``db.refresh`` after the single-flight token refresh commits — re-emits
    the right GUC via the begin listener) AND emits onto the live connection now,
    so an in-flight transaction picks up the scope immediately. The prior scope is
    restored on exit. The GUC emit is a no-op on non-Postgres (the SQLite test
    substrate cannot enforce RLS).
    """
    prev = _current_scope.set(_validated_scope(org_id, realm_id, bypass, company_id))
    try:
        emit_tenant_guc(db.connection(), _current_scope.get())
        yield
    finally:
        # Restore the DB GUC, not just the ContextVar. Resetting the ContextVar alone
        # leaves the block's GUC (e.g. ``rls_bypass='on'``) on the STILL-OPEN transaction —
        # a leak until the next emit_tenant_guc / commit (D1). Clear to the fail-closed
        # baseline, then re-emit the enclosing scope so the live GUC matches it EXACTLY:
        # ``emit_tenant_guc`` only SETS the keys a scope carries (and no-ops on a ``None``
        # scope, tenant_rls emit_tenant_guc: it does NOT clear), so the explicit clear is
        # what removes a key the enclosing scope does not carry / drops a leaked bypass.
        _current_scope.reset(prev)
        # ONLY touch the GUC if a transaction is still open on this session. If the block
        # committed (no active transaction), the ``is_local`` GUCs already cleared at the
        # commit and the begin listener emits the restored scope on the NEXT transaction.
        # Calling ``db.connection()`` unguarded would BEGIN a fresh transaction here under
        # the cleared scope and leave it open — making a post-block read fail-close to zero
        # (regressing the commit-inside-the-block callers: qbo token / celery write paths).
        if db.in_transaction():
            restored = _current_scope.get()
            connection = db.connection()
            clear_tenant_guc(connection)
            if restored is not None:
                emit_tenant_guc(connection, restored)


def pin_tenant_scope_live(db: "Session", *, org_id: int | None = None,
                          realm_id: str | None = None, bypass: bool = False,
                          company_id: int | None = None) -> None:
    """Imperative form of ``tenant_scope_live`` for a request handler.

    Pins the ContextVar AND emits the GUC onto ``db``'s current transaction now,
    WITHOUT auto-restore — the request teardown (``get_db``'s ``finally`` →
    ``clear_tenant_scope``) clears it, and the ``is_local`` GUC auto-clears at the
    transaction's end. Use this when a whole handler (e.g. a platform-admin
    cross-realm enumeration that never runs ``_check_company_access``) needs one
    tenant scope and a ``with`` block would force a large re-indent. No-op for the
    GUC on non-Postgres.
    """
    set_tenant_scope(org_id=org_id, realm_id=realm_id, bypass=bypass, company_id=company_id)
    emit_tenant_guc(db.connection(), _current_scope.get())


@contextmanager
def tenant_scope_for_realm(db: "Session", realm_id: "str | int") -> "Iterator[None]":
    """Pin BOTH the org and realm GUCs for a background/system unit of work that
    only knows the realm.

    A Celery task / system job naturally has just the realm (the QBO company id).
    But many tenant tables are **org**-keyed under live RLS (``qbo_tokens``,
    ``qbo_accounts``, ``qbo_reports``, ``company_memory``, …), so a realm-only
    scope leaves ``app.current_org_id`` unset and every org-keyed read/write fails
    closed (INCIDENT-qbo-tokens-rls.md, §B systemic class). This resolves the
    owning org from the realm via the RLS-EXCLUDED ``organization``/
    ``organization_companies`` tables (readable under any scope) and pins
    **org+realm** via ``tenant_scope_live`` (which also emits onto the already-open
    transaction — the org-resolution query opens it first). Falls back to
    realm-only when the realm has no org mapping (the builder/sweep then no-ops
    safely rather than crashing). Use this in place of
    ``tenant_scope(realm_id=…)`` anywhere a task touches org-keyed tables.
    """
    # Lazy import: tenant_companies → models → db → tenant_rls would cycle at
    # import time; importing here defers it to call time.
    from .services.tenant_companies import organization_for_company

    realm = str(realm_id)
    org = organization_for_company(db, realm)
    if org is not None:
        with tenant_scope_live(db, org_id=org.id, realm_id=realm):
            yield
    else:
        with tenant_scope_live(db, realm_id=realm):
            yield


def emit_tenant_guc(connection, scope: TenantScope | None) -> None:
    """Replace every transaction-local key, including keys omitted by the new scope.

    Numeric company is a separate axis from the historical QBO realm. Clearing on
    every repin/None also removes stale bypass and prior-company state.
    """
    if connection.dialect.name != "postgresql":
        return
    clear_tenant_guc(connection)
    if scope is None:
        return
    if scope.company_id is not None:
        _validated_scope(scope.org_id, scope.realm_id, scope.bypass, scope.company_id)
    if scope.bypass:
        connection.execute(text("SELECT set_config('app.rls_bypass', 'on', true)"))
        return
    for key, value in (("app.current_org_id", scope.org_id),
                       ("app.current_realm_id", scope.realm_id),
                       ("app.current_company_id", scope.company_id)):
        if value is not None:
            connection.execute(text(f"SELECT set_config('{key}', :v, true)"), {"v": str(value)})


def clear_tenant_guc(connection) -> None:
    """Fail-closed transaction-local baseline for all four tenant keys."""
    if connection.dialect.name != "postgresql":
        return
    for key, value in (("app.rls_bypass", "off"), ("app.current_org_id", ""),
                       ("app.current_realm_id", ""), ("app.current_company_id", "")):
        connection.execute(text(f"SELECT set_config('{key}', '{value}', true)"))


def install_rls_guc_listener(engine) -> None:
    """Register the per-transaction GUC emitter on a Postgres engine (cutover step)."""

    @event.listens_for(engine, "begin")
    def _on_begin(connection) -> None:
        emit_tenant_guc(connection, _current_scope.get())


# ─────────────────────── realm-bound statement enforcement ───────────────────────
#
# WHY THIS EXISTS. The RLS allowlist in migration ``e7f1a9c3d2b5`` holds 98 tables:
# 67 keyed on ``realm_id``/``company_id`` and 31 on ``organization_id``. Many of the
# org-keyed ones ALSO carry a realm column, and for those a statement issued under an
# ``org+realm`` pin returns EVERY realm in the organization unless the query itself
# filters — the policy predicate keys on ``organization_id``, which every member of the
# org satisfies. No error, no log, no denial. This check is the mechanical guard that
# "no statement spans realms" is not merely a query-authoring convention there.
#
# WHY IT ANALYSES THE COMPILED TREE AND NOT THE SQL TEXT. A previous implementation
# regex-matched the rendered SQL and was demonstrably unsound: `id IN (?, ?, ?)`,
# `realm_id != ?`, `id = ? OR organization_id = ?`, `NOT (id = ?)`, an identity term
# inside `ORDER BY CASE` or a scalar subquery, and an `IN` list containing the pinned
# realm ALONGSIDE others all passed it — including a cross-realm DELETE. Text cannot
# express "a TOP-LEVEL AND-conjunct binds this column to this value", and that sentence
# is the whole security property. See
# work/realm-bound-statement-enforcement/research/20260910-realm-unbound-statement-inventory/AC3-ADVERSARIAL-FINDINGS.md
#
# THE RULE, in one sentence: a statement touching a guard table passes only if the
# WHERE clause has a TOP-LEVEL AND-conjunct of the exact form
# ``<guard_table>.<realm_column> == <bind whose value is the pinned realm>``.
#
# That single criterion subsumes every bypass above, because a top-level AND-conjunct
# is a CEILING on the result set: whatever the other conjuncts do — OR, NOT, subquery,
# pagination — the rows returned are a subset of the pinned realm. Conversely a realm
# term under a disjunction or negation is a FLOOR, not a ceiling, and is correctly
# rejected because it is not a top-level conjunct.

_REALM_COLUMN_NAMES = ("realm_id", "company_id", "qbo_realm_id", "intended_realm_id")


@lru_cache(maxsize=1)
def _guard_tables() -> dict:
    """{table_name: realm_column} for every org-keyed table that carries a realm column.

    DERIVED from live model metadata, never hand-typed. The previous hardcoded list of
    19 had already drifted before it shipped: it missed five tables naming the column
    ``qbo_realm_id``/``intended_realm_id``, and ~12 more added by later migrations —
    including ``internal_action_executions``, the documented mirror of
    ``qbo_action_executions``, which WAS in the list. A table outside the set fails OPEN,
    so an incomplete set is a silent hole, which is exactly why this is computed.

    The rule is semantic, not a copy of the migration: a table is a guard table when it
    carries ``organization_id`` (so an org-keyed RLS policy cannot separate realms) AND a
    realm column (so realm separation is meaningful for it). This is a SUPERSET of the
    RLS allowlist — it also covers tables carrying no policy at all, where an unbounded
    read is if anything worse. Over-covering is the safe direction.
    """
    from .db import Base

    guard = {}
    for name, table in Base.metadata.tables.items():
        cols = {c.name for c in table.columns}
        if "organization_id" not in cols:
            continue
        for realm_col in _REALM_COLUMN_NAMES:
            if realm_col in cols:
                guard[name] = realm_col
                break
    return guard


@lru_cache(maxsize=None)
def _identity_columns(table_name: str) -> frozenset:
    """Columns whose EQUALITY pins the query to at most one row.

    Primary key, ``unique=True`` columns, and single-column UNIQUE constraints/indexes,
    from live metadata. Multi-column UNIQUE constraints are excluded: they bound a query
    only when every member column is bound (so ``qbo_accounts`` yields ``{id}`` alone,
    NOT ``realm_id``/``qbo_id``).
    """
    from .db import Base

    table = Base.metadata.tables.get(table_name)
    if table is None:
        return frozenset()
    cols = {c.name for c in table.primary_key.columns}
    cols |= {c.name for c in table.columns if c.unique}
    for constraint in table.constraints:
        if isinstance(constraint, UniqueConstraint) and len(constraint.columns) == 1:
            cols |= {c.name for c in constraint.columns}
    for index in table.indexes:
        if index.unique and len(index.columns) == 1:
            cols |= {c.name for c in index.columns}
    return frozenset(cols)


_org_grain: ContextVar = ContextVar("rls_org_grain_reason", default=None)

# Every org-grain block entered at runtime, for the close-time review the plan requires.
org_grain_uses: list = []


@contextmanager
def org_grain_read(reason: str):
    """Declare that the statements inside are deliberately ORG-grain, not realm-grain.

    THE EXEMPTION MECHANISM. A reviewed exemption must be CODE, not a line in a document:
    greppable, reviewable in a diff, and impossible to benefit from by accident. A doc
    entry cannot be enforced, cannot be found by the next reader, and silently widens as
    the function around it changes.

    The distinction this encodes: an org-grain read returns a NUMBER computed across the
    organization (a quota total, a bucket count, a realm enumeration the caller is entitled
    to). A realm-unbound read returns another realm's ROWS. This block asserts the former.
    It is NOT a way to silence the gate on a read you have not justified.

    Scope it as tightly as the justification: wrap the org-grain BRANCH, never the whole
    function, or the exemption widens to cases nobody reviewed.

    ``reason`` is required and recorded. Entering with a blank reason is a programming
    error, not an exemption.
    """
    if not reason or not reason.strip():
        raise ValueError("org_grain_read requires a substantive reason")
    scope = _current_scope.get()
    org_grain_uses.append({
        "reason": reason,
        "origin": _origin_frame(),
        "realm_at_entry": getattr(scope, "realm_id", None),
    })
    token = _org_grain.set(reason)
    try:
        yield
    finally:
        _org_grain.reset(token)


class RealmUnboundStatementError(RuntimeError):
    """A statement touched a guard table without binding the pinned realm."""


# Populated in "collect" mode; the discovery inventory.
realm_unbound_statements: list = []


def _table_name_of(column) -> str | None:
    """Underlying table name for a column, seeing through an alias."""
    table = getattr(column, "table", None)
    if table is None:
        return None
    element = getattr(table, "element", None)  # Alias -> the aliased selectable
    return getattr(element if element is not None else table, "name", None)


def _top_level_conjuncts(clause) -> list:
    """The clauses ANDed together at the TOP level of ``clause``.

    A top-level conjunct constrains the whole result set, so binding the realm in one is
    a ceiling. Anything under an OR or a NOT is deliberately NOT flattened into this list
    — that is the distinction the text-based predecessor could not make.
    """
    if clause is None:
        return []
    if isinstance(clause, BooleanClauseList) and clause.operator is operators.and_:
        out = []
        for child in clause.clauses:
            out.extend(_top_level_conjuncts(child))
        return out
    return [clause]


def _resolved_bind_value(element, compiled_params, bind_names=None):
    """The RUNTIME value of a bind. Never the cached literal.

    Load-bearing, and previously wrong in the most dangerous direction. The compiled
    statement is CACHED per SQL shape, and its BindParameter carries the literal from the
    FIRST execution of that shape in the process. Falling back to ``element.value`` therefore
    compares the pinned realm against whichever realm happened to run first — which both
    denied correct queries for every later realm AND, worse, PASSED a genuine cross-realm
    read whenever the cached literal happened to equal the pin. Verified: pinned to realm-A
    while filtering realm-B passed the gate.

    A bind whose runtime value cannot be resolved is UNRESOLVED, never a guess.
    """
    if not isinstance(element, BindParameter) or compiled_params is None:
        return _UNRESOLVED
    # A BindParameter's own `.key` is the anonymous construct-time name
    # ("%(140234 realm_id)s"); `compiled_parameters` is keyed by the COMPILED name
    # ("realm_id_1"). `compiled.bind_names` is the only correct translation between them.
    name = bind_names.get(element) if bind_names else None
    if name is not None and name in compiled_params:
        return compiled_params[name]
    if element.key in compiled_params:
        return compiled_params[element.key]
    return _UNRESOLVED


_UNRESOLVED = object()


def _eq_binding(conjunct, compiled_params, bind_names=None):
    """(table_name, column_name, bound_value) if ``conjunct`` is ``col == bind``, else None.

    Equality ONLY. ``IN`` is excluded deliberately: it returns N rows with N chosen by the
    caller (unbounded with a subquery), which falsifies the single-row premise. A previous
    version accepted ``IN`` and thereby allowed ``id IN (1..N)`` enumeration against dense
    integer primary keys.
    """
    if not isinstance(conjunct, BinaryExpression):
        return None
    if conjunct.operator is not operators.eq:
        return None
    for column, other in ((conjunct.left, conjunct.right), (conjunct.right, conjunct.left)):
        if not isinstance(column, ColumnClause):
            continue
        table_name = _table_name_of(column)
        if table_name is None:
            continue
        value = _resolved_bind_value(other, compiled_params, bind_names)
        if value is _UNRESOLVED:
            continue
        return table_name, column.name, value
    return None


def _direct_tables(element, out: set) -> None:
    """Tables a FROM element sources DIRECTLY, not descending into a nested SELECT.

    A Join contributes both sides. An Alias of a real Table is still that table. An Alias
    or Subquery wrapping a SELECT is a SEPARATE scope with its own WHERE, so it is skipped
    here and visited as its own scope by _statement_scopes.
    """
    if isinstance(element, Table):
        out.add(element.name)
        return
    inner = getattr(element, "element", None)
    if inner is not None:
        if isinstance(inner, Table):
            out.add(inner.name)
        return
    left, right = getattr(element, "left", None), getattr(element, "right", None)
    if left is not None:
        _direct_tables(left, out)
    if right is not None:
        _direct_tables(right, out)


def _statement_scopes(statement) -> list:
    """[(scope, tables it sources directly)] for every SELECT/DML level in the tree.

    WHY SCOPES AND NOT ONE STATEMENT. A guard table must be bound at the top level of the
    select that SOURCES it - not of the outermost statement, where such a conjunct is often
    impossible to write. Two shapes proved this, both from real code:

      * `.count()` renders as `SELECT count(*) FROM (SELECT ... WHERE ...)`. The outer
        statement has NO whereclause at all; the inner one carries the binding, and the
        inner WHERE genuinely IS a ceiling on the outer result because that subquery is the
        outer query's only source of rows.
      * `WHERE id NOT IN (SELECT agent_run_id FROM agent_episodes WHERE company_id = :r)`
        (app/tasks/memory.py:430). `agent_episodes` is sourced ONLY by that subquery, which
        binds its realm at its own top level. Demanding an outer conjunct for it asks for a
        predicate on a table the outer query does not select from.

    This does NOT reopen the subquery hole: each scope is still judged by ITS OWN top-level
    AND-conjuncts, so an UNBOUNDED outer select over a guard table is still denied even when
    an inner subquery over the same table is bound.
    """
    scopes = []
    for element in visitors.iterate(statement, {}):
        if isinstance(element, (Update, Delete, Insert)):
            table = getattr(element, "table", None)
            if table is not None:
                scopes.append((element, {table.name}))
        elif isinstance(element, Select):
            tables: set = set()
            try:
                froms = element.get_final_froms()
            except Exception:
                froms = []
            for from_element in froms:
                _direct_tables(from_element, tables)
            if tables:
                scopes.append((element, tables))
    return scopes


def _is_dml(statement) -> bool:
    return isinstance(statement, (Update, Delete, Insert))


def _insert_rows_bind_realm(param_rows, realm_col: str, pinned) -> bool:
    """Every row of an INSERT must set the realm column to the pinned realm.

    An ``Insert`` has no WHERE clause, so the top-level-conjunct rule is UNSATISFIABLE for
    it — no call-site edit could ever pass. The values-side equivalent of the ceiling is:
    every row this statement writes must name the pinned realm. That is strictly stronger
    than anything a WHERE could express, because it constrains what is CREATED rather than
    what is matched.

    A row that omits the realm column, or sets it NULL, is denied: an org-keyed guard table
    with a NULL realm is a row no realm-scoped read will ever bound correctly.

    Every row is checked, not just the first — an executemany can mix realms.
    """
    if not param_rows:
        return False
    for row in param_rows:
        if not isinstance(row, dict) or realm_col not in row:
            return False
        value = row[realm_col]
        if value is None or str(value) != str(pinned):
            return False
    return True


class CompanyUnboundStatementError(RuntimeError):
    """A neutral statement lacks both exact server-pinned ownership predicates."""


class CompanyUnboundFlushError(RuntimeError):
    """An ORM row does not carry the exact neutral ownership pair."""


def _company_guard_tables():
    # No cache: subsequently imported neutral models must not be silently missed.
    from .db import Base
    return {name for name, table in Base.metadata.tables.items()
            if table.info.get("review_company_scoped") is True}


def _company_pin_valid(scope):
    return (scope is not None and not scope.bypass and scope.realm_id is None and
            type(scope.org_id) is int and scope.org_id > 0 and
            type(scope.company_id) is int and scope.company_id > 0)


REVIEW_SCOPE_SETTINGS_SQL = (
    "SELECT current_setting('app.current_org_id', true),"
    "current_setting('app.current_company_id', true),"
    "current_setting('app.current_realm_id', true),"
    "current_setting('app.rls_bypass', true)"
)


def read_review_database_scope(db):
    """Read only the four fixed isolation keys on the current transaction."""
    if not _company_pin_valid(_current_scope.get()):
        raise CompanyUnboundStatementError("Scope inspection requires a numeric company pin")
    return tuple(db.execute(text(REVIEW_SCOPE_SETTINGS_SQL)).one())


def _neutral_control_sql(statement):
    # Only these internal GUC operations may use text under a neutral pin. No
    # arbitrary SQL/data query receives the legacy raw/PK/org-grain exemption.
    allowed = {REVIEW_SCOPE_SETTINGS_SQL}
    for key in ("app.current_org_id", "app.current_realm_id", "app.current_company_id", "app.rls_bypass"):
        allowed.add(f"SELECT set_config('{key}', :v, true)")
        for value in ("", "off", "on"):
            allowed.add(f"SELECT set_config('{key}', '{value}', true)")
    return str(statement).strip() in allowed


def check_company_compiled_statement(statement, param_rows, scope, *, mode="deny", bind_names=None):
    """Neutral tables always require org AND numeric company; legacy mode is irrelevant.

    ORM inserts are values-bound. Updates/deletes must use explicit scoped Core
    predicates; a validated ORM instance alone does not authorize PK-only SQL.
    This deliberate restriction avoids reusing the legacy single-PK shortcut.
    PostgreSQL FORCE-RLS remains the independent direct-SQL backstop.
    """
    if isinstance(statement, DDLElement):
        # Schema-owner bootstrap is distinct from tenant DML. A request pinned
        # to a numeric company has no schema authority, even for typed DDL.
        if scope is not None and scope.company_id is not None:
            raise CompanyUnboundStatementError("Company scope cannot authorize schema DDL")
        return
    guard = _company_guard_tables()
    # Alias collapse in the legacy analyser cannot prove each neutral alias's
    # ownership independently. Reject that unsupported shape instead of letting
    # one alias's predicate authorize a second alias or self-join.
    if statement is not None:
        for node in visitors.iterate(statement):
            raw_fragment = isinstance(node, TextClause) or (
                isinstance(node, ColumnClause) and getattr(node, "is_literal", False))
            if raw_fragment:
                raw = str(node)
                names_neutral = any(re.search(r"(?<![A-Za-z0-9_])" + re.escape(name) + r"(?![A-Za-z0-9_])", raw, re.I)
                                    for name in guard)
                numeric_pin = scope is not None and scope.company_id is not None
                fixed_control = node is statement and _neutral_control_sql(statement)
                trivial_literal = isinstance(node, ColumnClause) and raw in {"*", "1"}
                if names_neutral or (numeric_pin and not fixed_control and not trivial_literal):
                    raise CompanyUnboundStatementError("Raw SQL fragments cannot authorize neutral data")
            inner = getattr(node, "element", None)
            if isinstance(inner, Table) and inner.name in guard:
                raise CompanyUnboundStatementError("Neutral table aliases require an independently scoped query")
    scopes = _statement_scopes(statement) if statement is not None else []
    touched = {name for _, names in scopes for name in names if name in guard}
    if scope is not None and scope.company_id is not None:
        if any(names & _guard_tables().keys() for _, names in scopes):
            raise CompanyUnboundStatementError("Neutral scope does not authorize legacy realm data")
    if not scopes and statement is not None:
        raw = str(statement)
        touches_raw = any(re.search(r"(?<![A-Za-z0-9_])" + re.escape(name) + r"(?![A-Za-z0-9_])", raw, re.I)
                          for name in guard)
        if touches_raw or (scope is not None and scope.company_id is not None and not _neutral_control_sql(statement)):
            raise CompanyUnboundStatementError("Unanalysable SQL is not authorized for neutral company data")
        return
    if not touched:
        return
    if not _company_pin_valid(scope):
        raise CompanyUnboundStatementError("Neutral data requires an exact org/company pin without bypass")
    for selected, names in scopes:
        for name in names & guard:
            if isinstance(selected, Insert):
                if not (_insert_rows_bind_realm(param_rows, "organization_id", scope.org_id) and
                        _insert_rows_bind_realm(param_rows, "organization_company_id", scope.company_id)):
                    raise CompanyUnboundStatementError("Every neutral insert must bind both ownership values")
                continue
            for row in param_rows or [{}]:
                bindings = [value for value in (
                    _eq_binding(clause, row, bind_names)
                    for clause in _top_level_conjuncts(getattr(selected, "whereclause", None))) if value]
                for column, expected in (("organization_id", scope.org_id),
                                         ("organization_company_id", scope.company_id)):
                    if not any(t == name and c == column and str(v) == str(expected) for t, c, v in bindings):
                        raise CompanyUnboundStatementError("Neutral statements require both exact top-level ownership predicates")
                if isinstance(selected, Update):
                    values = getattr(selected, "_values", None)
                    immutable = {"id", "provider", "environment", "external_account_id", "external_entity_id",
                                 "connection_id", "external_book_id", "accounting_basis", "currency"}
                    if not values or getattr(selected, "_ordered_values", None):
                        raise CompanyUnboundStatementError("Neutral updates require an explicit reviewed values map")
                    # Execute-time column keys can add SET targets to a Core
                    # update. Reject immutable keys even when absent in _values.
                    if immutable.intersection(row):
                        raise CompanyUnboundStatementError("Retained source/book identity cannot be rewritten")
                    for owner, expected in (("organization_id", scope.org_id),
                                            ("organization_company_id", scope.company_id)):
                        if owner in row and row[owner] != expected:
                            raise CompanyUnboundStatementError("Neutral ownership cannot be reassigned")
                    for column, value in values.items():
                        key = getattr(column, "name", column)
                        if key in {"id", "provider", "environment", "external_account_id", "external_entity_id",
                                   "connection_id", "external_book_id", "accounting_basis", "currency"}:
                            raise CompanyUnboundStatementError("Retained source/book identity cannot be rewritten")
                        if key in ("organization_id", "organization_company_id"):
                            actual = _resolved_bind_value(value, row, bind_names)
                            expected = scope.org_id if key == "organization_id" else scope.company_id
                            if actual is _UNRESOLVED or str(actual) != str(expected):
                                raise CompanyUnboundStatementError("Neutral ownership cannot be reassigned")


def check_company_orm_flush(session, *, mode="deny"):
    scope = _current_scope.get()
    for instances, operation in ((session.new, "INSERT"), (session.dirty, "UPDATE"), (session.deleted, "DELETE")):
        for instance in instances:
            table = getattr(instance, "__table__", None)
            if (table is not None and scope is not None and scope.company_id is not None
                and table.name in _guard_tables()):
                raise CompanyUnboundFlushError("Neutral scope does not authorize legacy realm writes")
            if table is None or table.info.get("review_company_scoped") is not True:
                continue
            if operation == "UPDATE" and not session.is_modified(instance, include_collections=False):
                continue
            if (not _company_pin_valid(scope) or
                    getattr(instance, "organization_id", None) != scope.org_id or
                    getattr(instance, "organization_company_id", None) != scope.company_id):
                raise CompanyUnboundFlushError("Neutral ORM writes require the exact org/company owner, without bypass")
            # Identity changes must not rewrite retained source/book ownership.
            if operation == "UPDATE":
                from sqlalchemy import inspect
                state = inspect(instance)
                immutable = ("id", "organization_id", "organization_company_id", "provider", "environment",
                             "external_account_id", "external_entity_id", "connection_id", "external_book_id",
                             "accounting_basis", "currency")
                if any(key in state.attrs and state.attrs[key].history.has_changes() for key in immutable):
                    raise CompanyUnboundFlushError("Retained source/book identity is immutable")


def check_compiled_statement(statement, param_rows, scope, *, mode: str,
                             bind_names=None) -> None:
    """Enforce the realm-bound invariant against a compiled SQLAlchemy statement.

    Fires ONLY when the unit of work has pinned exactly one realm and is not bypassing, so
    platform-admin cross-tenant paths and unscoped bootstrap paths are unaffected by
    construction. A declared org_grain_read block is the one reviewed exemption.
    """
    check_company_compiled_statement(statement, param_rows, scope, mode=mode, bind_names=bind_names)
    if mode == "off" or scope is None or scope.bypass or not scope.realm_id:
        return
    if _org_grain.get():
        return
    if statement is None:
        return

    guard = _guard_tables()
    compiled_params = param_rows[0] if param_rows else None
    pinned = str(scope.realm_id)

    offenders = []
    for sel, sourced in _statement_scopes(statement):
        touched = sourced & guard.keys()
        if not touched:
            continue
        is_insert = isinstance(sel, Insert)
        bindings = [
            b
            for b in (
                _eq_binding(c, compiled_params, bind_names)
                for c in _top_level_conjuncts(getattr(sel, "whereclause", None))
            )
            if b
        ]
        for table_name in sorted(touched):
            realm_col = guard[table_name]

            # (0) INSERT has no WHERE, so the ceiling is values-side: every row written
            #     must name the pinned realm.
            if is_insert:
                if _insert_rows_bind_realm(param_rows, realm_col, pinned):
                    continue
                offenders.append(table_name)
                continue

            # (1) A top-level AND-conjunct binding this table's realm column is a CEILING
            #     on this scope's rows: whatever the other conjuncts do, the result is a
            #     subset of the pinned realm. The primary pass.
            if any(
                t == table_name and c == realm_col and str(v) == pinned
                for t, c, v in bindings
            ):
                continue

            # (2) A top-level equality on a PK/UNIQUE column pins ONE row, which cannot
            #     ENUMERATE another realm. This covers UPDATE/DELETE as well as SELECT:
            #     removing it made the rule unsatisfiable for the ORM, whose unit-of-work
            #     flush emits `UPDATE t SET <dirty> WHERE t.pk = ?` with no hook to add a
            #     conjunct. The realm on an ORM write is instead verified on the INSTANCE
            #     by install_realm_bound_orm_listener, which is stronger. Core DML by
            #     primary key carries the same residual object-reference risk as a SELECT
            #     by primary key, and no more.
            if any(
                t == table_name and c in _identity_columns(table_name)
                for t, c, _ in bindings
            ):
                continue

            offenders.append(table_name)

    if not offenders:
        return

    flat = " ".join(str(statement).split())
    if mode == "collect":
        for table_name in sorted(set(offenders)):
            realm_unbound_statements.append({
                "table": table_name,
                "realm_column": guard[table_name],
                "is_dml": _is_dml(statement),
                "statement": flat[:400],
                "origin": _origin_frame(),
            })
        return

    table_name = sorted(set(offenders))[0]
    raise RealmUnboundStatementError(
        f"statement sources guard table '{table_name}' while pinned to realm '{pinned}' "
        f"without a top-level AND-conjunct binding {table_name}.{guard[table_name]} to it, "
        f"in the SELECT that sources it. Under an org-keyed RLS policy this can return "
        f"every realm in the organization. Add the realm filter as a top-level condition "
        f"of that select (not inside an OR/NOT), or declare a reviewed org_grain_read "
        f"block. Statement: {flat[:300]}"
    )


def _origin_frame() -> str:
    """The ``file:line`` that issued the statement — first frame outside the plumbing."""
    _SKIP = ("/sqlalchemy/", "/_pytest/", "/pluggy/", "/site-packages/alembic/")
    for frame in reversed(traceback.extract_stack()[:-1]):
        name = frame.filename
        if any(part in name for part in _SKIP) or name.endswith("tenant_rls.py"):
            continue
        return f"{name}:{frame.lineno}"
    return "<unknown>"


def install_realm_bound_statement_listener(engine, *, mode: str | None = None) -> None:
    """Register the realm-bound statement check.

    ``mode``: ``deny`` raises, ``collect`` records to ``realm_unbound_statements``, ``off``
    disables. Env override: ``PEREGRINE_REALM_STMT_MODE``.

    A statement with no compiled construct (raw ``text()`` SQL) cannot be analysed and is
    NOT silently passed: it is treated as unanalysable and reported, so raw SQL against a
    guard table has to be made explicit rather than becoming an invisible hole.
    """
    resolved = mode or os.environ.get("PEREGRINE_REALM_STMT_MODE", "deny")

    if resolved == "collect":
        import atexit
        import json as _json

        def _dump() -> None:
            if not realm_unbound_statements:
                return
            seen, unique = set(), []
            for record in realm_unbound_statements:
                key = (record["table"], record.get("origin"), record["statement"])
                if key not in seen:
                    seen.add(key)
                    unique.append(record)
            out = os.environ.get("PEREGRINE_REALM_STMT_OUT", "realm-unbound-statements.json")
            with open(out, "w", encoding="utf-8") as handle:
                _json.dump({"total_hits": len(realm_unbound_statements),
                            "unique": len(unique), "records": unique}, handle, indent=2)

        atexit.register(_dump)

    @event.listens_for(engine, "before_cursor_execute")
    def _on_execute(conn, cursor, statement, parameters, context, executemany) -> None:
        compiled = getattr(context, "compiled", None)
        if compiled is None:
            # Driver SQL has no typed schema authority. It must pass the neutral
            # raw-SQL check too; PostgreSQL RLS is an additional boundary.
            scope = _current_scope.get()
            # SQLite's schema compiler checks table/index metadata with driver
            # PRAGMAs before typed DDL. This fixed grammar reads schema only,
            # never rows, and remains forbidden under numeric company scope.
            schema_pragma = re.fullmatch(
                r'PRAGMA (?:main|temp)\.(?:table_info|table_xinfo|foreign_key_list|index_list|index_info|index_xinfo)\("[A-Za-z0-9_]+"\)',
                statement.strip(), flags=re.I)
            if conn.dialect.name == "sqlite" and schema_pragma and not (scope and scope.company_id is not None):
                return
            check_company_compiled_statement(text(statement), parameters, scope)
            return
        # `compiled_parameters` (a list of param dicts, one per executemany row) is the
        # real SQLAlchemy 2.x attribute. `compiled_params` does NOT exist - reading it
        # silently yielded None on every execution and forced the cached-literal fallback.
        # The FULL list, not [0]: an executemany INSERT can mix realms across rows.
        rows = getattr(context, "compiled_parameters", None)
        check_compiled_statement(
            getattr(compiled, "statement", None),
            rows,
            _current_scope.get(),
            mode=resolved,
            bind_names=getattr(compiled, "bind_names", None),
        )


# ─────────────────────── ORM unit-of-work realm enforcement ───────────────────────
#
# WHY A SECOND MECHANISM. A SQLAlchemy unit-of-work flush emits
# ``UPDATE t SET <dirty columns> WHERE t.pk = ?`` and offers NO hook to add a WHERE
# conjunct, so the statement-level "name your realm" rule is unsatisfiable for every
# ORM write in the codebase — no call-site edit could comply.
#
# The realm on an ORM write is instead checked where it actually lives: on the mapped
# INSTANCE, before the flush emits anything. That is strictly stronger than a WHERE
# conjunct, because a conjunct only constrains which row is matched while this
# constrains the realm the row actually carries — it catches a write that targets the
# right row and sets the WRONG realm, which no WHERE clause could.


class RealmUnboundFlushError(RuntimeError):
    """A flush would write a guard-table row belonging to another realm."""


def check_orm_flush(session, *, mode: str) -> None:
    """Assert every pending guard-table instance carries the pinned realm."""
    scope = _current_scope.get()
    check_company_orm_flush(session, mode=mode)
    if mode == "off" or scope is None or scope.bypass or not scope.realm_id:
        return
    if _org_grain.get():
        return

    guard = _guard_tables()
    offenders = []
    for instances, operation in (
        (session.new, "INSERT"),
        (session.dirty, "UPDATE"),
        (session.deleted, "DELETE"),
    ):
        for instance in instances:
            table = getattr(getattr(instance, "__table__", None), "name", None)
            if table not in guard:
                continue
            # `session.dirty` is pessimistic — it lists instances that MAY have changed.
            if operation == "UPDATE" and not session.is_modified(
                instance, include_collections=False
            ):
                continue
            realm_col = guard[table]
            value = getattr(instance, realm_col, None)
            if value is not None and str(value) == str(scope.realm_id):
                continue
            offenders.append((operation, table, realm_col, value))

    if not offenders:
        return

    if mode == "collect":
        for operation, table, realm_col, value in offenders:
            realm_unbound_statements.append({
                "table": table,
                "realm_column": realm_col,
                "is_dml": True,
                "statement": f"ORM FLUSH {operation} (instance.{realm_col}={value!r})",
                "origin": _origin_frame(),
            })
        return

    operation, table, realm_col, value = offenders[0]
    raise RealmUnboundFlushError(
        f"flush would {operation} a '{table}' row whose {realm_col} is {value!r} while "
        f"pinned to realm '{scope.realm_id}'. An ORM write must carry the pinned realm on "
        f"the instance. ({len(offenders)} offending instance(s) in this flush.)"
    )


def install_realm_bound_orm_listener(target=None, *, mode: str | None = None) -> None:
    """Register the unit-of-work realm check.

    ``target`` defaults to the ``Session`` CLASS, so it covers every session in the
    process; pass a specific sessionmaker/Session to scope it (tests do this).
    """
    from sqlalchemy.orm import Session as _Session

    resolved = mode or os.environ.get("PEREGRINE_REALM_STMT_MODE", "deny")

    @event.listens_for(target if target is not None else _Session, "before_flush")
    def _on_before_flush(session, flush_context, instances) -> None:
        check_orm_flush(session, mode=resolved)

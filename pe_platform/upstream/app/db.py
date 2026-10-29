import logging

from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy.pool import NullPool

from .config import settings
from .tenant_rls import (
    clear_tenant_scope,
    install_realm_bound_orm_listener,
    install_realm_bound_statement_listener,
    install_rls_guc_listener,
)

logger = logging.getLogger(__name__)

if not settings.database_url:
    raise RuntimeError("DATABASE_URL is not set")

_is_sqlite = settings.database_url.startswith("sqlite")
_engine_kwargs = {"future": True}
if not _is_sqlite:
    if settings.db_pooler == "transaction":
        # NullPool: one server connection per checkout, no in-process pooling.
        # MUST ONLY be enabled behind a transaction-pooling PgBouncer that multiplexes
        # the app's checkouts onto a small server pool. On a DIRECT DB connection this
        # opens a fresh Postgres connection per checkout and can exhaust max_connections
        # under load. NullPool rejects pool_size/max_overflow/pool_timeout (TypeError),
        # so they are omitted; pool_recycle is moot without pooling. pool_pre_ping is
        # honored by NullPool and kept (liveness check on checkout).  # SA-OK: NullPool behind PgBouncer transaction pooling (DB_POOLER=transaction)
        _engine_kwargs.update(
            {
                "poolclass": NullPool,
                "pool_pre_ping": settings.db_pool_pre_ping,
            }
        )
    else:
        # DEFAULT ("queue"): today's bounded in-process QueuePool — byte-unchanged.
        _engine_kwargs.update(
            {
                "pool_size": settings.db_pool_size,
                "max_overflow": settings.db_max_overflow,
                "pool_timeout": settings.db_pool_timeout,
                "pool_recycle": settings.db_pool_recycle,
                "pool_pre_ping": settings.db_pool_pre_ping,
            }
        )

engine = create_engine(settings.database_url, **_engine_kwargs)

# Wire the RLS backstop GUC emitter (Wave B): sets app.current_org_id /
# app.current_realm_id per transaction from the session-resolved tenant scope.
# No-op on SQLite and harmless until the policy migration is applied (unused GUCs).
install_rls_guc_listener(engine)

# AC-4: realm-bound enforcement, DENY by default.
#
# 49 guard tables are org-keyed in RLS but carry a realm column, so their policy predicate
# (organization_id) cannot separate realms - every member of the org satisfies it. These two
# listeners are what actually hold that line:
#   - statement-level: a read/UPDATE/DELETE needs a TOP-LEVEL AND-conjunct binding the
#     table's realm column to the pinned realm (or, for SELECT/PK-bounded DML, an identity
#     equality); an INSERT needs every written row to name the pinned realm.
#   - unit-of-work: an ORM flush emits `UPDATE ... WHERE pk = ?` with no hook for a
#     conjunct, so the realm is checked on the mapped INSTANCE instead - which also catches
#     a write that targets the right row and sets the WRONG realm.
# Deliberate org-grain reads declare themselves with `org_grain_read(reason)`; that is the
# only exemption, and it is code rather than a document entry.
install_realm_bound_statement_listener(engine)
install_realm_bound_orm_listener()


def ensure_pgvector_extension() -> None:
    """Best-effort pgvector extension init, called once at app startup (NOT at
    import — so there is no import-time DB I/O). Uses a real logged handler instead
    of a silent swallow, so a pgvector-init failure surfaces (DB-001)."""
    from sqlalchemy import text as sa_text
    try:
        with engine.connect() as conn:
            conn.execute(sa_text("CREATE EXTENSION IF NOT EXISTS vector"))  # VECTOR-OK: best-effort startup init with a logged handler; the ordered-migration move is AC-D2
            conn.commit()
    except (OperationalError, ProgrammingError) as exc:
        logger.warning("pgvector extension not enabled (vector ops disabled): %s", type(exc).__name__)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()   # <-- this defines Base

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
        clear_tenant_scope()  # don't let a request's tenant scope leak onto a pooled thread

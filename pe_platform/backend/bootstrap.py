"""One isolated local runtime per process; upstream modules remain unchanged."""

from __future__ import annotations

import json
import os
import secrets
import sys
import threading
from pathlib import Path

from fastapi import Depends, Request, Response

from .security import LocalSecurity, LocalSecurityMiddleware
from .workspace import WorkspaceService

_runtime = None


class Runtime:
    def active_workspace(self):
        return self.workspaces.active()

    def require_scope(self, request):
        return self.security.require_scope(request)

    def auth_for_scope(self, db, scope):
        from app.auth import bind_session_organization
        from app.auth_session import AuthContext
        from app.models import Organization, OrganizationMembership, User

        user = db.query(User).filter(User.id == scope["user_id"]).one()
        org = db.query(Organization).filter(Organization.id == scope["organization_id"]).one()
        member = (
            db.query(OrganizationMembership)
            .filter(
                OrganizationMembership.user_id == user.id, OrganizationMembership.organization_id == org.id
            )
            .one()
        )
        return bind_session_organization(
            AuthContext(user=user, organization=org, membership=member, auth_source="local-device", token="")
        )

    def user_for_scope(self, db, scope):
        return self.auth_for_scope(db, scope).user


def configure(data_dir, port=8880, frontend_origin=None, testing=False):
    global _runtime
    directory = Path(data_dir).expanduser().resolve()
    if _runtime is not None:
        if _runtime.data_dir != directory or _runtime.port != port:
            raise RuntimeError(
                "A platform process owns one explicit data directory and port. Start a separate process for another instance."
            )
        return _runtime
    if "app.config" in sys.modules or "app.db" in sys.modules:
        raise RuntimeError("Configure the isolated local platform before importing upstream app modules.")
    directory.mkdir(parents=True, exist_ok=True)
    if (directory / "forecast.sqlite3").exists():
        raise RuntimeError(
            "This is a legacy cash-flow data directory. Select a new platform data directory; legacy versions are preserved separately."
        )
    from .data_lock import acquire_data_lock

    data_lock = acquire_data_lock(directory)
    try:
        runtime = _create_runtime(directory, port, frontend_origin, testing)
    except BaseException:
        data_lock.close()
        raise
    runtime.data_lock = data_lock
    _runtime = runtime
    return runtime


def _create_runtime(directory, port, frontend_origin, testing):
    # Only the child backend's explicit local configuration reaches native Settings.
    # Values are never read or printed. Provider credentials live in scoped settings.
    prefixes = (
        "QBO_",
        "OPENAI_",
        "ANTHROPIC_",
        "STYTCH_",
        "STRIPE_",
        "REDIS_",
        "CELERY_",
        "OBJECT_STORAGE_",
        "GEMINI_",
        "GOOGLE_",
        "DEEPSEEK_",
        "GROQ_",
        "TOGETHER_",
    )
    for name in tuple(os.environ):
        if name.startswith(prefixes) or name in {
            "DATABASE_URL",
            "VESPER_BOT_DOTENV_PATH",
            "SECRET_KEY",
            "FERNET_KEY",
        }:
            os.environ.pop(name, None)
    os.environ.update(
        {
            "ENVIRONMENT": "test" if testing else "development",
            "DATABASE_URL": "sqlite:///" + str(directory / "platform.sqlite3"),
            "SECRET_KEY": secrets.token_urlsafe(48),
            "PYTHON_DOTENV_DISABLED": "1",
            "REDIS_URL": "",
            "CELERY_BROKER_URL": "memory://",
            "CELERY_RESULT_BACKEND": "cache+memory://",
            "CORS_ORIGINS": frontend_origin or f"http://127.0.0.1:{port}",
            "VESPER_BOT_REALM_STMT_MODE": "deny",
        }
    )
    upstream = Path(__file__).resolve().parents[1] / "upstream"
    sys.path.insert(0, str(upstream))
    from app import db, models  # noqa: F401 — registers native mappings
    from app.config import settings as native_settings

    native_settings.openai_api_key = ""
    native_settings.anthropic_api_key = ""
    from vesper_cashflow.settings import SettingsService

    runtime = Runtime()
    runtime.data_dir, runtime.port, runtime.testing = directory, port, testing
    runtime.financial_lock = threading.RLock()
    runtime.session_factory = db.SessionLocal
    runtime.engine = db.engine
    runtime.settings = SettingsService(directory, testing=testing)
    runtime.workspaces = WorkspaceService(runtime)
    runtime.security = LocalSecurity(runtime, port, frontend_origin, testing)
    runtime.source_revision = json.loads((upstream.parent / "source-manifest.json").read_text())[
        "source_revision"
    ]
    from sqlalchemy import event

    @event.listens_for(db.engine, "connect")
    def local_sqlite_guards(connection, record):
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")

    db.Base.metadata.create_all(bind=db.engine)
    with db.engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TRIGGER IF NOT EXISTS immutable_local_forecast_export_update BEFORE UPDATE ON cash_forecast_export_snapshots BEGIN SELECT RAISE(ABORT,'Forecast export snapshots are immutable'); END"
        )
        connection.exec_driver_sql(
            "CREATE TRIGGER IF NOT EXISTS immutable_local_forecast_export_delete BEFORE DELETE ON cash_forecast_export_snapshots BEGIN SELECT RAISE(ABORT,'Forecast export snapshots are immutable'); END"
        )
    return runtime


def get_runtime():
    if _runtime is None:
        raise RuntimeError("Configure the local PE platform runtime first.")
    return _runtime


def register(app):
    from app import auth, db as native_db

    runtime = get_runtime()
    if getattr(app.state, "pe_runtime", None) is not None:
        return runtime
    app.state.pe_runtime = runtime
    app.add_middleware(LocalSecurityMiddleware, runtime=runtime)

    def get_db():
        session = runtime.session_factory()
        try:
            yield session
        finally:
            session.close()
            from app.tenant_rls import clear_tenant_scope

            clear_tenant_scope()

    def get_auth(request: Request, db=Depends(get_db)):
        return runtime.auth_for_scope(db, runtime.require_scope(request))

    def get_user(request: Request, db=Depends(get_db)):
        return runtime.user_for_scope(db, runtime.require_scope(request))

    app.dependency_overrides[native_db.get_db] = get_db
    app.dependency_overrides[auth.get_current_auth_context] = get_auth
    app.dependency_overrides[auth.get_current_user_from_token] = get_user

    @app.get("/local/bootstrap")
    def bootstrap(response: Response):
        response.set_cookie(
            runtime.security.cookie_name,
            runtime.security.session_token,
            httponly=True,
            samesite="strict",
            secure=False,
            path="/",
        )
        return {
            "workspace": runtime.workspaces.active(),
            "workspaces": runtime.workspaces.all(),
            "csrf_token": runtime.security.csrf_token,
            "data_directory": str(runtime.data_dir),
            "source_revision": runtime.source_revision,
            "identity_mode": "Local device operator; no hosted login or independently verified human identity",
        }

    @app.post("/local/workspaces")
    async def create_workspace(request: Request):
        body = await request.json()
        with runtime.financial_lock:
            result = runtime.workspaces.create(body.get("name"), body.get("currency", "USD"))
        return {"workspace": result}

    @app.post("/local/workspaces/select")
    async def select_workspace(request: Request):
        body = await request.json()
        return {"workspace": runtime.workspaces.select(body.get("id"))}

    @app.post("/local/demo")
    def demo():
        from .fixtures import create_demo

        with runtime.financial_lock:
            return {"workspace": create_demo(runtime)}

    from . import connections, cashflow

    connections.register(app, runtime)
    cashflow.register(app, runtime)
    return runtime

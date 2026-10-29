"""Server-owned local workspace identities, separate from all legacy databases."""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager

from fastapi import HTTPException


class WorkspaceService:
    def __init__(self, runtime):
        self.runtime = runtime
        self.path = runtime.data_dir / "workspaces.sqlite3"
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS workspaces (
                    id TEXT PRIMARY KEY, organization_id INTEGER NOT NULL,
                    organization_company_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
                    realm_id TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
                    currency TEXT NOT NULL, synthetic INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS selection (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    workspace_id TEXT, revision INTEGER NOT NULL
                );
                INSERT OR IGNORE INTO selection VALUES (1,NULL,0);
            """)

    @contextmanager
    def connection(self):
        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            yield db

    def active(self):
        with self.connection() as db:
            row = db.execute(
                "SELECT w.*,s.revision FROM workspaces w JOIN selection s ON s.workspace_id=w.id WHERE s.singleton=1"
            ).fetchone()
            return self._public(row) if row else None

    @staticmethod
    def _public(row):
        value = dict(row)
        value["synthetic"] = bool(value["synthetic"])
        return value

    def all(self):
        with self.connection() as db:
            revision = db.execute("SELECT revision FROM selection WHERE singleton=1").fetchone()[0]
            return [
                self._public({**dict(row), "revision": revision})
                for row in db.execute("SELECT * FROM workspaces ORDER BY rowid")
            ]

    def by_realm(self, realm_id):
        return next((row for row in self.all() if row["realm_id"] == str(realm_id)), None)

    def require_current(self, scope):
        active = self.active()
        if not active or any(
            active.get(key) != scope.get(key) for key in ("id", "revision", "realm_id", "organization_id")
        ):
            raise HTTPException(409, "The displayed workspace changed; reload before continuing.")
        return active

    def select(self, workspace_id):
        with self.runtime.financial_lock, self.connection() as db:
            if not db.execute("SELECT 1 FROM workspaces WHERE id=?", (workspace_id,)).fetchone():
                raise HTTPException(404, "Local workspace not found")
            db.execute(
                "UPDATE selection SET workspace_id=?,revision=revision+1 WHERE singleton=1", (workspace_id,)
            )
        return self.active()

    def create(self, name, currency="USD", *, synthetic=False):
        from app.models import Organization, OrganizationCompany, OrganizationMembership, User
        from app.tenant_rls import tenant_scope

        if not isinstance(name, str) or not name.strip() or len(name) > 120:
            raise HTTPException(400, "Enter an entity name of 1–120 characters.")
        if currency != "USD":
            raise HTTPException(
                400,
                "This native PE cash-flow workspace supports USD only. Legacy currency workspaces remain separate; no FX conversion is performed.",
            )
        wid = uuid.uuid4().hex
        realm_id = "synthetic-" + wid if synthetic else "local-" + wid
        with self.runtime.financial_lock, tenant_scope(bypass=True), self.runtime.session_factory() as db:
            # These are local device identities, not hosted accounts or legal assent.
            org = Organization(
                stytch_organization_id="local-org-" + wid,
                slug="local-" + wid,
                name=name.strip(),
                primary_realm_id=realm_id,
                status="active",
                is_active=True,
            )
            db.add(org)
            db.flush()
            user = User(
                email=f"local-{wid}@device.invalid",
                password_hash="local-device-session-only",
                role="admin",
                display_name="Local device operator",
                auth_source="local-device",
                legacy_login_enabled=False,
                is_priority_admin=False,
                default_organization_id=org.id,
            )
            db.add(user)
            db.flush()
            company = OrganizationCompany(
                organization_id=org.id,
                realm_id=realm_id,
                display_name=name.strip(),
                is_primary=True,
                status="active",
            )
            db.add(company)
            db.flush()
            db.add(
                OrganizationMembership(
                    organization_id=org.id,
                    user_id=user.id,
                    stytch_member_id="local-member-" + wid,
                    email=user.email,
                    role="ADMIN",
                    status="active",
                    is_default=True,
                )
            )
            db.commit()
            record = (wid, org.id, company.id, user.id, realm_id, name.strip(), currency, int(synthetic))
        self.runtime.settings.register_new_workspace(wid)
        with self.connection() as db:
            db.execute("INSERT INTO workspaces VALUES (?,?,?,?,?,?,?,?)", record)
            db.execute("UPDATE selection SET workspace_id=?,revision=revision+1 WHERE singleton=1", (wid,))
        return self.active()

    def bind_realm(self, scope, realm_id):
        from app.models import (
            CashForecastActual,
            CashForecastOverride,
            CashForecastSnapshot,
            Organization,
            OrganizationCompany,
        )
        from app.tenant_rls import tenant_scope

        with self.runtime.financial_lock:
            self.require_current(scope)
            if scope["synthetic"]:
                raise HTTPException(400, "Create your own entity before connecting real accounting data.")
            if scope["realm_id"] == realm_id:
                return scope
            if not scope["realm_id"].startswith("local-"):
                raise HTTPException(409, "Use a new workspace to connect a different company.")
            if self.by_realm(realm_id):
                raise HTTPException(
                    409, "That accounting company already belongs to another local workspace."
                )
            with (
                tenant_scope(org_id=scope["organization_id"], realm_id=scope["realm_id"]),
                self.runtime.session_factory() as db,
            ):
                for model in (CashForecastActual, CashForecastOverride, CashForecastSnapshot):
                    if db.query(model).filter(model.company_id == scope["realm_id"]).first():
                        raise HTTPException(
                            409,
                            "This workspace contains financial work; connect the company in a new workspace.",
                        )
                company = (
                    db.query(OrganizationCompany)
                    .filter(
                        OrganizationCompany.id == scope["organization_company_id"],
                        OrganizationCompany.organization_id == scope["organization_id"],
                    )
                    .one()
                )
                org = db.query(Organization).filter(Organization.id == scope["organization_id"]).one()
                company.realm_id = realm_id
                org.primary_realm_id = realm_id
                # Authorize/read the empty old binding first, then pin this
                # identity-only flush to its explicitly confirmed new realm.
                # Native write guards remain enabled throughout the rebind.
                with tenant_scope(org_id=scope["organization_id"], realm_id=realm_id):
                    db.commit()
            with self.connection() as db:
                db.execute("UPDATE workspaces SET realm_id=? WHERE id=?", (realm_id, scope["id"]))
                db.execute("UPDATE selection SET revision=revision+1 WHERE singleton=1")
            return self.active()

    def source_path(self, scope):
        folder = self.runtime.data_dir / "source-evidence"
        folder.mkdir(exist_ok=True)
        return folder / (scope["id"] + ".json")

    def read_source(self, scope):
        path = self.source_path(scope)
        return json.loads(path.read_text()) if path.exists() else {"reads": {}}

    def write_source(self, scope, value):
        path = self.source_path(scope)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False), encoding="utf-8"
        )
        temporary.replace(path)

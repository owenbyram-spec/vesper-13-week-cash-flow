"""SQLite scope, immutable snapshots, hash verification and atomic approvals."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .models import canonical, digest, normalize_inputs, text
from .projection import project
from .proposals import apply_changes, comparison


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "forecast.sqlite3"
        self.lock = threading.RLock()
        with self.connection() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS workspaces(id TEXT PRIMARY KEY, name TEXT NOT NULL, currency TEXT NOT NULL, synthetic INTEGER NOT NULL, active_version TEXT, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS versions(id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES workspaces(id), sequence INTEGER NOT NULL, label TEXT NOT NULL, created_at TEXT NOT NULL, parent_id TEXT, inputs TEXT NOT NULL, input_hash TEXT NOT NULL, result TEXT NOT NULL, result_hash TEXT NOT NULL, UNIQUE(workspace_id,sequence));
                CREATE TRIGGER IF NOT EXISTS immutable_versions BEFORE UPDATE ON versions BEGIN SELECT RAISE(ABORT,'Forecast versions are immutable'); END;
                CREATE TABLE IF NOT EXISTS proposals(id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES workspaces(id), base_id TEXT NOT NULL, base_hash TEXT NOT NULL, proposal_hash TEXT NOT NULL, label TEXT NOT NULL, changes TEXT NOT NULL, preview TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL, resolved_at TEXT, approved_version TEXT);
                CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, workspace_id TEXT NOT NULL, event TEXT NOT NULL, details TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            """)

    @contextmanager
    def connection(self):
        with self.lock:
            db = sqlite3.connect(self.path, timeout=10)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            try:
                yield db
                db.commit()
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()

    def _event(self, db, workspace_id: str, event: str, details: dict):
        db.execute(
            "INSERT INTO events(workspace_id,event,details,created_at) VALUES (?,?,?,?)",
            (workspace_id, event, canonical(details), now()),
        )

    def list_workspaces(self) -> list[dict]:
        with self.connection() as db:
            return [dict(row) for row in db.execute("SELECT * FROM workspaces ORDER BY created_at DESC")]

    def create_workspace(self, name: str, currency: str = "USD", synthetic: bool = False) -> dict:
        from .domain.money import Money

        name = text(name, "Entity name", 120)
        Money.zero(currency)
        workspace_id = uuid.uuid4().hex
        with self.connection() as db:
            db.execute(
                "INSERT INTO workspaces VALUES(?,?,?,?,?,?)",
                (workspace_id, name, currency, int(synthetic), None, now()),
            )
            db.execute("INSERT OR REPLACE INTO state VALUES('active_workspace',?)", (workspace_id,))
            self._event(db, workspace_id, "workspace_created", {"synthetic": synthetic})
        return self.active_workspace()

    def active_workspace(self) -> dict | None:
        with self.connection() as db:
            row = db.execute(
                "SELECT w.* FROM workspaces w JOIN state s ON s.value=w.id WHERE s.key='active_workspace'"
            ).fetchone()
            return dict(row) if row else None

    def activate(self, workspace_id: str) -> dict:
        with self.connection() as db:
            if not db.execute("SELECT 1 FROM workspaces WHERE id=?", (workspace_id,)).fetchone():
                raise ValueError("Unknown local workspace")
            db.execute("INSERT OR REPLACE INTO state VALUES('active_workspace',?)", (workspace_id,))
        return self.active_workspace()

    def require_scope(self, scope: dict | None = None) -> dict:
        current = self.active_workspace()
        if not current:
            raise ValueError("Create or open a workspace first")
        if scope and scope["id"] != current["id"]:
            raise ValueError("Active workspace changed; reload before continuing")
        if scope and (
            ("active_version" in scope and scope["active_version"] != current["active_version"])
            or ("version" in scope and scope["version"] != current["active_version"])
        ):
            raise ValueError("Active forecast version changed; reload before continuing")
        return current

    def _read_version(self, row) -> dict:
        if not row:
            raise ValueError("No forecast version exists in this workspace")
        version = dict(row)
        version["inputs"] = json.loads(version["inputs"])
        version["result"] = json.loads(version["result"])
        result = dict(version["result"])
        claimed = result.pop("result_hash", None)
        if (
            digest(version["inputs"]) != version["input_hash"]
            or digest(result) != claimed
            or claimed != version["result_hash"]
            or version["result"].get("input_hash") != version["input_hash"]
        ):
            raise ValueError(
                "Saved version integrity check failed; preserve this database and restore a verified backup"
            )
        return version

    def get_version(self, version_id: str | None = None, scope: dict | None = None) -> dict:
        current = self.require_scope(scope)
        with self.connection() as db:
            return self._read_version(
                db.execute(
                    "SELECT * FROM versions WHERE id=? AND workspace_id=?",
                    (version_id or current["active_version"], current["id"]),
                ).fetchone()
            )

    def list_versions(self, scope: dict | None = None) -> list[dict]:
        current = self.require_scope(scope)
        with self.connection() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT id,sequence,label,created_at,parent_id,input_hash,result_hash FROM versions WHERE workspace_id=? ORDER BY sequence DESC",
                    (current["id"],),
                )
            ]

    def _save(self, db, scope: dict, inputs: dict, label: str, parent_id: str | None) -> dict:
        if inputs["currency"] != scope["currency"]:
            raise ValueError("Input currency differs from the active workspace")
        result = project(inputs)
        sequence = db.execute(
            "SELECT COALESCE(MAX(sequence),0)+1 FROM versions WHERE workspace_id=?", (scope["id"],)
        ).fetchone()[0]
        version_id, created = uuid.uuid4().hex, now()
        db.execute(
            "INSERT INTO versions VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                version_id,
                scope["id"],
                sequence,
                text(label, "Version label", 120),
                created,
                parent_id,
                canonical(inputs),
                digest(inputs),
                canonical(result),
                result["result_hash"],
            ),
        )
        db.execute("UPDATE workspaces SET active_version=? WHERE id=?", (version_id, scope["id"]))
        self._event(
            db,
            scope["id"],
            "version_saved",
            {"id": version_id, "input_hash": digest(inputs), "parent_id": parent_id},
        )
        return {
            "id": version_id,
            "workspace_id": scope["id"],
            "sequence": sequence,
            "label": label,
            "created_at": created,
            "parent_id": parent_id,
            "inputs": inputs,
            "input_hash": digest(inputs),
            "result": result,
            "result_hash": result["result_hash"],
        }

    def save_version(
        self, raw: dict, label: str, *, base_hash: str | None = None, scope: dict | None = None
    ) -> dict:
        inputs = normalize_inputs(raw)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            current = self.require_scope(scope)
            if current["active_version"]:
                prior = self._read_version(
                    db.execute(
                        "SELECT * FROM versions WHERE id=? AND workspace_id=?",
                        (current["active_version"], current["id"]),
                    ).fetchone()
                )
                if base_hash != prior["input_hash"]:
                    raise ValueError("Stale input version; reload and review the current forecast")
            elif base_hash is not None:
                raise ValueError("Unexpected base hash for a first version")
            return self._save(db, current, inputs, label, current["active_version"])

    def activate_version(self, version_id: str, scope: dict | None = None) -> dict:
        current = self.require_scope(scope)
        version = self.get_version(version_id, current)
        with self.connection() as db:
            self.require_scope(current)
            db.execute("UPDATE workspaces SET active_version=? WHERE id=?", (version_id, current["id"]))
            self._event(db, current["id"], "version_opened", {"id": version_id})
        return version

    def draft_proposal(
        self, changes: list[dict], label: str, *, base_hash: str, scope: dict | None = None
    ) -> dict:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            current = self.require_scope(scope)
            version = self.get_version(scope=current)
            if version["input_hash"] != base_hash:
                raise ValueError("Stale base version; regenerate this proposal")
            changed = apply_changes(version["inputs"], changes)
            preview = project(changed)
            proposal_id = uuid.uuid4().hex
            bound = {
                "workspace_id": current["id"],
                "base_id": version["id"],
                "base_hash": base_hash,
                "changes": changes,
            }
            proposal_hash = digest(bound)
            db.execute(
                "INSERT INTO proposals VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    proposal_id,
                    current["id"],
                    version["id"],
                    base_hash,
                    proposal_hash,
                    text(label, "Proposal label", 120),
                    canonical(changes),
                    canonical(preview),
                    "pending",
                    now(),
                    None,
                    None,
                ),
            )
            self._event(
                db,
                current["id"],
                "proposal_drafted",
                {"id": proposal_id, "proposal_hash": proposal_hash, "base_hash": base_hash},
            )
        return {
            "id": proposal_id,
            "base_id": version["id"],
            "base_hash": base_hash,
            "proposal_hash": proposal_hash,
            "label": label,
            "changes": changes,
            "preview": preview,
            "comparison": comparison(version["result"], preview),
            "status": "pending",
        }

    def list_proposals(self, scope: dict | None = None) -> list[dict]:
        current = self.require_scope(scope)
        with self.connection() as db:
            rows = db.execute(
                "SELECT * FROM proposals WHERE workspace_id=? ORDER BY created_at DESC LIMIT 100",
                (current["id"],),
            ).fetchall()
            results = []
            for row in rows:
                proposal = dict(row)
                proposal["changes"] = json.loads(proposal["changes"])
                proposal["preview"] = json.loads(proposal["preview"])
                base = self._read_version(
                    db.execute(
                        "SELECT * FROM versions WHERE id=? AND workspace_id=?",
                        (proposal["base_id"], current["id"]),
                    ).fetchone()
                )
                proposal["comparison"] = comparison(base["result"], proposal["preview"])
                proposal["stale"] = proposal["base_id"] != current["active_version"]
                results.append(proposal)
            return results

    def resolve_proposal(
        self, proposal_id: str, proposal_hash: str, approve: bool, scope: dict | None = None
    ) -> dict:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            current = self.require_scope(scope)
            row = db.execute(
                "SELECT * FROM proposals WHERE id=? AND workspace_id=?", (proposal_id, current["id"])
            ).fetchone()
            if not row or row["status"] != "pending":
                raise ValueError("Unknown or already resolved proposal")
            changes = json.loads(row["changes"])
            if (
                row["proposal_hash"] != proposal_hash
                or digest(
                    {
                        "workspace_id": current["id"],
                        "base_id": row["base_id"],
                        "base_hash": row["base_hash"],
                        "changes": changes,
                    }
                )
                != proposal_hash
            ):
                raise ValueError("Proposal integrity mismatch; reload exact review bytes")
            if current["active_version"] != row["base_id"]:
                raise ValueError("Stale proposal: active version changed; regenerate before approval")
            base = self.get_version(scope=current)
            if base["input_hash"] != row["base_hash"]:
                raise ValueError("Stale proposal base hash")
            version = None
            if approve:
                changed = apply_changes(base["inputs"], changes)
                preview = project(changed)
                if canonical(preview) != row["preview"]:
                    raise ValueError("Proposal preview integrity mismatch")
                version = self._save(db, current, changed, row["label"], base["id"])
            db.execute(
                "UPDATE proposals SET status=?,resolved_at=?,approved_version=? WHERE id=?",
                (
                    "approved" if approve else "rejected",
                    now(),
                    version["id"] if version else None,
                    proposal_id,
                ),
            )
            self._event(
                db,
                current["id"],
                "proposal_approved" if approve else "proposal_rejected",
                {"id": proposal_id, "proposal_hash": proposal_hash},
            )
            return {"status": "approved" if approve else "rejected", "version": version}

    def backup(self) -> dict:
        current = self.require_scope()
        versions = [self.get_version(row["id"], current) for row in reversed(self.list_versions(current))]
        with self.connection() as db:
            events = [
                dict(row)
                for row in db.execute(
                    "SELECT event,details,created_at FROM events WHERE workspace_id=? ORDER BY id",
                    (current["id"],),
                )
            ]
        payload = {
            "backup_schema": 1,
            "application": "vesper-13-week-cash-flow",
            "exported_at": now(),
            "workspace": current,
            "versions": versions,
            "proposals": self.list_proposals(current),
            "events": events,
        }
        payload["backup_hash"] = digest(payload)
        return payload

    def restore(self, payload: dict) -> dict:
        if not isinstance(payload, dict) or len(canonical(payload).encode()) > 20_000_000:
            raise ValueError("Backup must be a JSON object smaller than 20 MB")
        claimed = payload.get("backup_hash")
        body = {key: value for key, value in payload.items() if key != "backup_hash"}
        if (
            payload.get("backup_schema") != 1
            or payload.get("application") != "vesper-13-week-cash-flow"
            or digest(body) != claimed
        ):
            raise ValueError("Unsupported or corrupted backup")
        versions = payload.get("versions")
        if not isinstance(versions, list) or not 1 <= len(versions) <= 1000:
            raise ValueError("Backup needs 1–1,000 forecast versions")
        old = payload.get("workspace", {})
        validated = []
        old_ids = set()
        for version in versions:
            normalized = normalize_inputs(version["inputs"])
            calculated = project(normalized)
            if (
                digest(normalized) != version["input_hash"]
                or calculated != version["result"]
                or calculated["result_hash"] != version["result_hash"]
            ):
                raise ValueError("Backup forecast verification failed; no data was restored")
            if (
                normalized["currency"] != old.get("currency")
                or version["id"] in old_ids
                or (version.get("parent_id") is not None and version["parent_id"] not in old_ids)
            ):
                raise ValueError("Backup version chain or currency is invalid")
            old_ids.add(version["id"])
            validated.append((version, normalized))
        if old.get("active_version") not in old_ids:
            raise ValueError("Backup active version is missing")
        # Every snapshot is verified before starting a single atomic restore.
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            new_id = uuid.uuid4().hex
            name = text(f"{old.get('name', 'Restored entity')} (restored)", "Restored entity", 150)
            db.execute(
                "INSERT INTO workspaces VALUES(?,?,?,?,?,?)",
                (new_id, name, old["currency"], int(bool(old.get("synthetic"))), None, now()),
            )
            scope = {"id": new_id, "currency": old["currency"]}
            ids = {}
            for version, normalized in validated:
                new = self._save(db, scope, normalized, version["label"], ids.get(version.get("parent_id")))
                ids[version["id"]] = new["id"]
            db.execute(
                "UPDATE workspaces SET active_version=? WHERE id=?", (ids[old["active_version"]], new_id)
            )
            db.execute("INSERT OR REPLACE INTO state VALUES('active_workspace',?)", (new_id,))
            self._event(
                db,
                new_id,
                "backup_restored",
                {
                    "backup_hash": claimed,
                    "versions": len(validated),
                    "original_proposals_retained_in_backup_only": len(payload.get("proposals", [])),
                },
            )
        return self.active_workspace()

    def delete_workspace(self, confirmation: str, scope: dict | None = None):
        current = self.require_scope(scope)
        if confirmation != current["name"]:
            raise ValueError("Type the active entity name exactly to delete its local data")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self.require_scope(current)
            db.execute("DELETE FROM proposals WHERE workspace_id=?", (current["id"],))
            db.execute("DELETE FROM versions WHERE workspace_id=?", (current["id"],))
            db.execute("DELETE FROM events WHERE workspace_id=?", (current["id"],))
            db.execute("DELETE FROM workspaces WHERE id=?", (current["id"],))
            db.execute("DELETE FROM state WHERE key='active_workspace'")

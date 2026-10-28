"""Workspace-bound credentials. No ambient keys or plaintext fallback."""

from __future__ import annotations

import json
import os
import tempfile
import re
import threading
from hashlib import sha256
from pathlib import Path

DEFAULT_MODEL = "gpt-5.6-terra"


class SettingsError(ValueError):
    pass


class MemorySecretStore:
    """Injected test store; also the explicit session-only fallback."""

    persistent = False

    def __init__(self):
        self.values = {}
        self.lock = threading.RLock()

    def get(self, key):
        with self.lock:
            return self.values.get(key)

    def set(self, key, value):
        with self.lock:
            self.values[key] = value

    def delete(self, key):
        with self.lock:
            self.values.pop(key, None)


class KeyringSecretStore:
    persistent = True

    def __init__(self, service):
        import keyring

        self.backend = keyring.get_keyring()
        module = type(self.backend).__module__.lower()
        # Explicit OS stores only. Third-party plaintext/fallback stores are refused.
        if not any(
            name in module
            for name in (
                "keyring.backends.macos",
                "keyring.backends.windows",
                "keyring.backends.secretservice",
                "keyring.backends.kwallet",
            )
        ):
            raise SettingsError("The OS credential store is unavailable; use session-only storage.")
        self.service = service

    def get(self, key):
        try:
            return self.backend.get_password(self.service, key)
        except Exception:
            raise SettingsError(
                "The OS credential store could not be read; unlock it or reconnect for this session."
            ) from None

    def set(self, key, value):
        try:
            self.backend.set_password(self.service, key, value)
        except Exception:
            raise SettingsError(
                "The OS credential store could not save the credentials; choose session-only storage."
            ) from None

    def delete(self, key):
        try:
            if self.backend.get_password(self.service, key) is not None:
                self.backend.delete_password(self.service, key)
        except Exception:
            raise SettingsError(
                "The stored credentials could not be removed; unlock the OS credential store and retry."
            ) from None


class SettingsService:
    def __init__(self, data_dir: Path, secret_store=None, *, testing=False):
        self.lock = threading.RLock()
        self.session = MemorySecretStore()
        self._store = secret_store
        self.testing = testing
        self.service = "Vesper.local." + sha256(str(data_dir.resolve()).encode()).hexdigest()[:20]
        self.storage_warning = ""
        # A session-only rotation must never depend on keychain availability.
        self.storage_modes = {}
        self.unverified_persistent = set()
        self.absence_file = Path(data_dir) / "credential-absence.json"
        self.known_absent = set()
        if self.absence_file.exists():
            try:
                if self.absence_file.stat().st_size > 2_000_000:
                    raise ValueError()
                record = json.loads(self.absence_file.read_text(encoding="utf-8"))
                entries = record.get("absent")
                if (
                    set(record) != {"schema", "absent"}
                    or record["schema"] != 1
                    or not isinstance(entries, list)
                    or len(entries) > 20000
                    or any(
                        not isinstance(key, str) or not re.fullmatch(r"[a-f0-9]{64}:(?:model|qbo)", key)
                        for key in entries
                    )
                ):
                    raise ValueError()
                self.known_absent = set(entries)
                self.storage_modes.update({key: "absent" for key in entries})
            except (OSError, ValueError, TypeError, AttributeError):
                # Unreadable metadata can never hide an actual OS credential entry.
                self.storage_warning = "Credential storage choices could not be read. Unlock the OS credential store before removing an existing connection."

    def _write_absence(self, entries):
        """Only hashed names of entries proven absent; never credential values."""
        temporary = None
        try:
            self.absence_file.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.absence_file.parent,
                prefix=".credential-absence-",
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                json.dump({"schema": 1, "absent": sorted(entries)}, handle, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(self.absence_file)
        except OSError:
            raise SettingsError(
                "Credential storage choices could not be saved locally. Check the application data directory permissions and retry."
            ) from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        self.known_absent = set(entries)

    def register_new_workspace(self, workspace_id):
        """Call only immediately after the app generates a new workspace identity."""
        if not isinstance(workspace_id, str) or not re.fullmatch(
            r"(?:workspace_)?[a-f0-9]{32}", workspace_id
        ):
            raise SettingsError("Register only a newly generated local workspace identity.")
        with self.lock:
            keys = {self._key(workspace_id, kind) for kind in ("model", "qbo")}
            if any(key in self.storage_modes or self.session.get(key) is not None for key in keys):
                raise SettingsError(
                    "This workspace credential identity is already known; do not register it again."
                )
            self._write_absence(self.known_absent | keys)
            self.storage_modes.update({key: "absent" for key in keys})

    def _remember_absence(self, key):
        if key not in self.known_absent:
            self._write_absence(self.known_absent | {key})

    def _forget_absence(self, key):
        if key in self.known_absent:
            self._write_absence(self.known_absent - {key})

    def _persistent(self):
        if self._store is None:
            if self.testing:
                self._store = MemorySecretStore()
            else:
                try:
                    self._store = KeyringSecretStore(self.service)
                except (SettingsError, ImportError):
                    self.storage_warning = (
                        "OS credential storage is unavailable. Choose session-only storage."
                    )
                    return None
        return self._store

    @staticmethod
    def _key(workspace_id, kind):
        if not isinstance(workspace_id, str) or not workspace_id or len(workspace_id) > 200:
            raise SettingsError("Select a valid workspace first.")
        return sha256(workspace_id.encode()).hexdigest() + ":" + kind

    def get(self, workspace_id, kind):
        with self.lock:
            key = self._key(workspace_id, kind)
            value = self.session.get(key)
            if value is None and self.storage_modes.get(key) not in {"session-only", "absent"}:
                store = self._persistent()
                try:
                    value = store.get(key) if store else None
                except SettingsError:
                    # Reading status must not prevent choosing explicit session storage.
                    self.storage_warning = (
                        "The OS credential store could not be read. Unlock it to use saved "
                        "connections, or reconnect for this session. Existing saved credentials "
                        "have not been removed."
                    )
                    value = None
                else:
                    if store is not None:
                        self.storage_modes[key] = "persistent" if value and store.persistent else "absent"
            try:
                return json.loads(value) if value else None
            except (ValueError, TypeError):
                raise SettingsError("Saved credentials are unreadable. Remove them and reconnect.") from None

    def save(self, workspace_id, kind, bundle, persist=False):
        with self.lock:
            key = self._key(workspace_id, kind)
            encoded = json.dumps(bundle, separators=(",", ":"))
            if persist:
                store = self._persistent()
                if store is None or not store.persistent:
                    raise SettingsError(
                        "Secure persistent storage is unavailable. Choose session-only storage explicitly."
                    )
                # Remove any absence claim before an OS write can succeed.
                self._forget_absence(key)
                self.storage_modes.pop(key, None)
                # Save the full rotated bundle as one OS entry before updating memory.
                store.set(key, encoded)
                self.storage_modes[key] = "persistent"
                self.unverified_persistent.discard(key)
            else:
                prior_mode = self.storage_modes.get(key)
                if prior_mode == "persistent":
                    store = self._persistent()
                    if store is None:
                        raise SettingsError(
                            "Unlock the OS credential store and retry to remove the saved copy "
                            "before switching this connection to session-only storage."
                        )
                    try:
                        store.delete(key)
                    except SettingsError:
                        raise SettingsError(
                            "The saved OS credential copy could not be removed. Unlock the "
                            "credential store and retry; storage has not changed."
                        ) from None
                    self._remember_absence(key)
                    self.unverified_persistent.discard(key)
                elif prior_mode is None:
                    # Do not probe an unavailable/locked keychain to save a new session.
                    # If prior persistence was never observed, disclose that uncertainty.
                    self.unverified_persistent.add(key)
                self.storage_modes[key] = "session-only"
            self.session.set(key, encoded)
            return "os-credential-store" if persist else "session-only"

    def replace(self, workspace_id, kind, bundle):
        with self.lock:
            key = self._key(workspace_id, kind)
            mode = self.storage_modes.get(key)
            if mode is None:
                # Establish the mode only for bundles not already selected this session.
                self.get(workspace_id, kind)
                mode = self.storage_modes.get(key)
            return self.save(workspace_id, kind, bundle, mode == "persistent")

    def remove(self, workspace_id, kind):
        with self.lock:
            key = self._key(workspace_id, kind)
            if (
                self.storage_modes.get(key) not in {"session-only", "absent"}
                or key in self.unverified_persistent
            ):
                store = self._persistent()
                if store is None:
                    raise SettingsError(
                        "The OS credential store is unavailable, so any saved copy could not "
                        "be removed. Unlock the store and retry."
                    )
                store.delete(key)
            self._remember_absence(key)
            self.session.delete(key)
            self.storage_modes[key] = "absent"
            self.unverified_persistent.discard(key)

    @staticmethod
    def _text(value, label, maximum=500):
        if not isinstance(value, str) or not value.strip() or len(value) > maximum:
            raise SettingsError(f"Enter a valid {label}.")
        return value.strip()

    def set_model(self, workspace_id, body):
        key = self._text(body.get("api_key"), "API key", 1000)
        model = self._text(body.get("model", DEFAULT_MODEL), "model name", 100)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,99}", model):
            raise SettingsError("The model name contains unsupported characters.")
        if body.get("consent") is not True:
            raise SettingsError("Review and accept the model data-transfer and usage notice first.")
        return self.save(
            workspace_id,
            "model",
            {"api_key": key, "model": model, "consent": True},
            body.get("persist") is True,
        )

    def set_qbo(self, workspace_id, body):
        bundle = {
            name: self._text(body.get(name), label, limit)
            for name, label, limit in (
                ("client_id", "Intuit Client ID", 500),
                ("client_secret", "Intuit Client secret", 1000),
                ("realm_id", "Realm ID", 30),
                ("refresh_token", "refresh token", 3000),
            )
        }
        if not re.fullmatch(r"[0-9]{1,30}", bundle["realm_id"]):
            raise SettingsError("Realm ID must be the numeric ID returned by Intuit.")
        bundle["environment"] = body.get("environment", "sandbox")
        if bundle["environment"] not in {"sandbox", "production"}:
            raise SettingsError("Choose sandbox or production.")
        return self.save(workspace_id, "qbo", bundle, body.get("persist") is True)

    def public(self, workspace_id):
        model, qbo = self.get(workspace_id, "model"), self.get(workspace_id, "qbo")
        return {
            "model": {
                "configured": bool(model),
                "model": model["model"] if model else DEFAULT_MODEL,
                "consent": bool(model and model.get("consent")),
            },
            "qbo": {
                "configured": bool(qbo),
                "environment": qbo.get("environment") if qbo else None,
                "bound": bool(qbo and qbo.get("bound_company")),
                "company": qbo.get("bound_company") if qbo else None,
            },
            "storage_warning": (
                "This connection is available for this session. Previously saved OS credentials, "
                "if any, were not checked or removed. Unlock the credential store and remove the "
                "connection to clear a saved copy."
                if any(
                    self._key(workspace_id, kind) in self.unverified_persistent for kind in ("model", "qbo")
                )
                else self.storage_warning
            ),
            "storage": "OS credential store when selected; otherwise this process session only",
        }

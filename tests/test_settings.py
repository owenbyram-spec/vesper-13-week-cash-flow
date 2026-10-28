import pytest

from vesper_cashflow.settings import MemorySecretStore, SettingsError, SettingsService


def model(**extra):
    return {
        "api_key": "synthetic-secret",
        "model": "synthetic-model",
        "consent": True,
        **extra,
    }


def test_workspace_credentials_never_appear_in_public_output(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "ambient-must-never-be-used")
    settings = SettingsService(tmp_path, MemorySecretStore(), testing=True)
    assert not settings.public("a")["model"]["configured"]
    assert settings.set_model("a", model()) == "session-only"
    assert settings.get("b", "model") is None
    assert "synthetic-secret" not in str(settings.public("a"))
    assert "ambient-must-never-be-used" not in str(settings.public("a"))
    assert list(tmp_path.iterdir()) == []
    settings.remove("a", "model")
    assert settings.get("a", "model") is None


def test_no_silent_plaintext_fallback_or_unconsented_model(tmp_path):
    settings = SettingsService(tmp_path, MemorySecretStore(), testing=True)
    with pytest.raises(SettingsError, match="session-only"):
        settings.set_model("a", model(persist=True))
    with pytest.raises(SettingsError, match="notice"):
        settings.set_model("a", model(consent=False))
    with pytest.raises(SettingsError):
        settings.set_qbo(
            "a",
            {
                "client_id": "x",
                "client_secret": "y",
                "refresh_token": "z",
                "realm_id": "../other",
                "environment": "sandbox",
            },
        )
    assert list(tmp_path.iterdir()) == []


def test_rotated_bundle_atomic_replacement_retains_storage_mode(tmp_path):
    class SecureTestStore(MemorySecretStore):
        persistent = True

    store = SecureTestStore()
    settings = SettingsService(tmp_path, store, testing=True)
    original = {
        "client_id": "synthetic",
        "client_secret": "synthetic",
        "realm_id": "123",
        "refresh_token": "old",
        "environment": "sandbox",
    }
    settings.save("a", "qbo", original, True)
    settings.replace(
        "a",
        "qbo",
        {**original, "refresh_token": "rotated", "access_token": "synthetic"},
    )
    restarted = SettingsService(tmp_path, store, testing=True)
    assert restarted.get("a", "qbo")["refresh_token"] == "rotated"
    assert len(store.values) == 1
    settings.save("a", "qbo", original, False)
    assert not store.values
    assert SettingsService(tmp_path, store, testing=True).get("a", "qbo") is None


class LockedSecretStore(MemorySecretStore):
    persistent = True

    def __init__(self):
        super().__init__()
        self.calls = []

    def get(self, key):
        self.calls.append("get")
        raise SettingsError("The test OS credential store is locked")

    def set(self, key, value):
        self.calls.append("set")
        raise SettingsError("The test OS credential store is locked")

    def delete(self, key):
        self.calls.append("delete")
        raise SettingsError("The test OS credential store is locked")


def test_explicit_new_session_storage_does_not_call_locked_os_backend(tmp_path):
    locked = LockedSecretStore()
    settings = SettingsService(tmp_path, locked, testing=True)
    assert settings.set_model("new-workspace", model(persist=False)) == "session-only"
    assert settings.get("new-workspace", "model")["api_key"] == "synthetic-secret"
    assert locked.calls == []
    # An unobserved old OS entry is not falsely described as removed.
    assert "not checked or removed" in settings.public("new-workspace")["storage_warning"]
    assert list(tmp_path.iterdir()) == []


def test_session_only_rotated_qbo_bundle_survives_keychain_lock(tmp_path):
    settings = SettingsService(tmp_path, MemorySecretStore(), testing=True)
    assert settings.get("a", "qbo") is None  # Known to have no old saved copy.
    original = {"refresh_token": "synthetic-old", "environment": "sandbox"}
    settings.save("a", "qbo", original, False)
    locked = LockedSecretStore()
    settings._store = locked
    assert settings.replace("a", "qbo", {**original, "refresh_token": "synthetic-rotated"}) == "session-only"
    assert settings.get("a", "qbo")["refresh_token"] == "synthetic-rotated"
    settings.remove("a", "qbo")
    assert settings.get("a", "qbo") is None
    assert locked.calls == []


def test_switching_known_persistent_copy_requires_successful_removal(tmp_path):
    class SecureTestStore(MemorySecretStore):
        persistent = True

    secure = SecureTestStore()
    settings = SettingsService(tmp_path, secure, testing=True)
    settings.set_model("a", model(persist=True))
    locked = LockedSecretStore()
    settings._store = locked
    with pytest.raises(SettingsError, match="saved OS credential copy could not be removed"):
        settings.set_model("a", model(api_key="replacement", persist=False))
    assert settings.get("a", "model")["api_key"] == "synthetic-secret"
    assert len(secure.values) == 1
    settings._store = secure
    assert settings.set_model("a", model(api_key="replacement", persist=False)) == "session-only"
    assert not secure.values
    assert settings.get("a", "model")["api_key"] == "replacement"


def test_locked_status_read_still_allows_explicit_session_reconnect(tmp_path):
    locked = LockedSecretStore()
    settings = SettingsService(tmp_path, locked, testing=True)
    assert settings.get("a", "qbo") is None
    assert "have not been removed" in settings.storage_warning
    locked.calls.clear()
    settings.save("a", "qbo", {"refresh_token": "synthetic"}, False)
    settings.replace("a", "qbo", {"refresh_token": "synthetic-rotated"})
    assert settings.get("a", "qbo")["refresh_token"] == "synthetic-rotated"
    assert locked.calls == []
    with pytest.raises(SettingsError, match="locked"):
        settings.remove("a", "qbo")
    assert settings.get("a", "qbo")["refresh_token"] == "synthetic-rotated"


def test_new_empty_workspace_can_be_removed_with_locked_keychain_after_restart(tmp_path):
    workspace_id = "c" * 32
    locked = LockedSecretStore()
    settings = SettingsService(tmp_path, locked, testing=True)
    settings.register_new_workspace(workspace_id)
    settings.remove(workspace_id, "model")
    settings.remove(workspace_id, "qbo")
    restarted = SettingsService(tmp_path, locked, testing=True)
    restarted.remove(workspace_id, "model")
    restarted.remove(workspace_id, "qbo")
    assert restarted.get(workspace_id, "model") is None
    assert restarted.get(workspace_id, "qbo") is None
    assert locked.calls == []
    metadata = (tmp_path / "credential-absence.json").read_text()
    assert workspace_id not in metadata and "synthetic-secret" not in metadata
    assert len(list(tmp_path.iterdir())) == 1


def test_registered_absence_never_hides_later_persisted_credentials(tmp_path):
    class SecureTestStore(MemorySecretStore):
        persistent = True

    workspace_id = "workspace_" + "d" * 32
    secure = SecureTestStore()
    settings = SettingsService(tmp_path, secure, testing=True)
    settings.register_new_workspace(workspace_id)
    with pytest.raises(SettingsError, match="already known"):
        settings.register_new_workspace(workspace_id)
    settings.set_model(workspace_id, model(persist=True))
    metadata = (tmp_path / "credential-absence.json").read_text()
    assert settings._key(workspace_id, "model") not in metadata
    restarted = SettingsService(tmp_path, secure, testing=True)
    assert restarted.get(workspace_id, "model")["api_key"] == "synthetic-secret"
    locked = LockedSecretStore()
    restarted._store = locked
    with pytest.raises(SettingsError, match="locked"):
        restarted.remove(workspace_id, "model")
    # A genuinely old, unknown workspace still cannot claim removal through a locked store.
    with pytest.raises(SettingsError, match="locked"):
        restarted.remove("e" * 32, "model")


def test_invalid_absence_metadata_does_not_hide_unknown_os_entries(tmp_path):
    class SecureTestStore(MemorySecretStore):
        persistent = True

    secure = SecureTestStore()
    settings = SettingsService(tmp_path, secure, testing=True)
    settings.set_model("old-workspace", model(persist=True))
    (tmp_path / "credential-absence.json").write_text('{"schema":1,"absent":["bad-key"]}')
    restarted = SettingsService(tmp_path, secure, testing=True)
    assert restarted.get("old-workspace", "model")["api_key"] == "synthetic-secret"
    assert "could not be read" in restarted.storage_warning

from start_platform import browser_build_identity, clean_environment


def test_prepared_browser_build_binds_source_lock_and_api(tmp_path):
    (tmp_path / "app").mkdir()
    page = tmp_path / "app/page.tsx"
    page.write_text("original")
    lock = tmp_path / "package-lock.json"
    lock.write_text("lock1")
    before = browser_build_identity(tmp_path, "http://127.0.0.1:8771")
    (tmp_path / ".next").mkdir()
    (tmp_path / ".next/cache").write_text("ignored")
    assert browser_build_identity(tmp_path, "http://127.0.0.1:8771") == before
    assert browser_build_identity(tmp_path, "http://127.0.0.1:8781") != before
    page.write_text("updated")
    assert browser_build_identity(tmp_path, "http://127.0.0.1:8771") != before
    page.write_text("original")
    lock.write_text("lock2")
    assert browser_build_identity(tmp_path, "http://127.0.0.1:8771") != before


def test_child_environment_excludes_ambient_credentials_and_network_overrides(monkeypatch):
    for name in ["OPENAI_API_KEY", "ANTHROPIC_API_KEY", "DATABASE_URL", "HTTP_PROXY", "NODE_OPTIONS"]:
        monkeypatch.setenv(name, "test-only-ambient-value")
    child = clean_environment()
    assert not any(name in child for name in ["OPENAI_API_KEY", "ANTHROPIC_API_KEY", "DATABASE_URL", "HTTP_PROXY", "NODE_OPTIONS"])
    assert child["NEXT_TELEMETRY_DISABLED"] == "1"

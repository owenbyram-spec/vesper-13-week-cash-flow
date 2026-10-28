"""Launcher behavior with no package downloads, credential access or live server."""

import hashlib
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

import run as launcher


class Probe:
    def __init__(self, occupied=False):
        self.occupied = occupied
        self.options = []
        self.address = None
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True

    def setsockopt(self, *option):
        self.options.append(option)

    def bind(self, address):
        self.address = address
        if self.occupied:
            raise OSError("Synthetic occupied local port")


def prevent_dependency_work(monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("Port checks/prepared launches must not prepare or install dependencies")

    monkeypatch.setattr(launcher, "venv", SimpleNamespace(EnvBuilder=unexpected))
    monkeypatch.setattr(launcher, "subprocess", SimpleNamespace(run=unexpected))


@pytest.mark.parametrize("platform", ["posix", "nt"])
def test_occupied_port_fails_before_environment_preparation_and_closes_probe(tmp_path, monkeypatch, platform):
    # The nt branch is simulated only; this does not execute the Windows launcher.
    root = tmp_path / "not-yet-extracted-environment"
    monkeypatch.setattr(launcher, "ROOT", root)
    monkeypatch.setattr(launcher, "os", SimpleNamespace(name=platform))
    argv = ["run.py", "--port", "8876"]
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(launcher, "sys", SimpleNamespace(version_info=(3, 12), argv=argv))
    probe = Probe(occupied=True)
    real_socket = launcher.socket
    monkeypatch.setattr(launcher, "socket", SimpleNamespace(
        socket=lambda: probe, SOL_SOCKET=real_socket.SOL_SOCKET, SO_REUSEADDR=real_socket.SO_REUSEADDR,
    ))
    prevent_dependency_work(monkeypatch)
    with pytest.raises(SystemExit, match="Local port 8876 is in use") as stopped:
        launcher.main()
    assert "same default data directory" in str(stopped.value)
    assert "No process was stopped" in str(stopped.value)
    assert probe.address == ("127.0.0.1", 8876)
    assert probe.closed is True
    assert probe.options == ([(real_socket.SOL_SOCKET, real_socket.SO_REUSEADDR, 1)] if platform == "posix" else [])
    assert not root.exists()


@pytest.mark.parametrize("requested", ["client-data/../finance-data", "~/vesper-launcher-verification", None])
def test_launcher_prints_resolved_instance_before_creating_app(tmp_path, monkeypatch, capsys, requested):
    root = tmp_path / "Downloaded CFO tool"
    env_root = root / ".venv"
    (env_root / "bin").mkdir(parents=True)
    (env_root / "bin/python").touch()
    lock = root / "requirements.lock"
    lock.write_text("synthetic prepared dependency lock\n")
    (env_root / ".vesper-dependencies").write_text(hashlib.sha256(lock.read_bytes()).hexdigest() + "\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(launcher, "ROOT", root)
    monkeypatch.setattr(launcher, "os", SimpleNamespace(name="posix"))
    argv = ["run.py", "--port", "8876", "--no-browser", "--no-install"]
    if requested is not None:
        argv.extend(["--data-dir", requested])
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(launcher, "sys", SimpleNamespace(version_info=(3, 12), argv=argv, prefix=str(env_root)))
    probe = Probe()
    monkeypatch.setattr(launcher, "socket", SimpleNamespace(
        socket=lambda: probe, SOL_SOCKET=1, SO_REUSEADDR=2,
    ))
    prevent_dependency_work(monkeypatch)
    default_directory = Path("~/vesper-default-launcher-verification")
    expected_directory = Path(requested).expanduser().resolve() if requested is not None else default_directory.expanduser().resolve()
    created = []
    app = object()

    def create_app(**kwargs):
        assert probe.closed is True
        displayed = capsys.readouterr().out
        assert f"App folder: {root}\n" in displayed
        assert f"Data directory: {expected_directory}\n" in displayed
        assert "Local address: http://127.0.0.1:8876\n" in displayed
        created.append(kwargs)
        return app

    web = SimpleNamespace(default_data_dir=lambda: default_directory, create_app=create_app)

    def import_web(name):
        assert name == "vesper_cashflow.web"
        return web

    monkeypatch.setattr(launcher, "importlib", SimpleNamespace(import_module=import_web))
    served = []
    monkeypatch.setitem(sys.modules, "uvicorn", SimpleNamespace(run=lambda instance, **kwargs: served.append((instance, kwargs))))
    launcher.main()
    assert created == [{"data_dir": expected_directory, "port": 8876, "testing": False}]
    assert served == [(app, {"host": "127.0.0.1", "port": 8876, "access_log": False, "log_level": "warning"})]
    assert probe.options == [(1, 2, 1)]

#!/usr/bin/env python3
"""Start this Vesper application in its own environment; never install globally."""

from __future__ import annotations
import argparse
import hashlib
import importlib
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
import venv
import webbrowser

PACKAGE = "vesper_cashflow"
MODULE = "web"
DEFAULT_PORT = 8766
ROOT = Path(__file__).resolve().parent


def main():
    if sys.version_info < (3, 12):
        raise SystemExit("Install Python 3.12 or newer from https://www.python.org/downloads/ and try again.")
    parser = argparse.ArgumentParser(description="Run 13-Week Cash Flow locally")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--data-dir", type=Path, help="Optional explicit local application-data directory")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument(
        "--no-install", action="store_true", help="Require the already prepared local environment"
    )
    parser.add_argument(
        "--testing",
        action="store_true",
        help="Isolated verification only; requires --data-dir and uses memory secrets",
    )
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        raise SystemExit("Choose a local port from 1024 through 65535.")
    if args.testing and not args.data_dir:
        raise SystemExit("Verification mode requires an explicit --data-dir.")
    # Fail before creating an environment or downloading packages for another copy.
    try:
        with socket.socket() as probe:
            if os.name != "nt":
                # Match the server on Unix: a recently closed connection's
                # TIME_WAIT state must not prevent reopening this same app.
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(("127.0.0.1", args.port))
    except OSError:
        raise SystemExit(
            f"Local port {args.port} is in use. Check the other copy's entity and data directory, "
            "or use --port with another number. A different port still uses the same default data "
            "directory unless you also choose --data-dir. No process was stopped."
        )
    env_root = ROOT / ".venv"
    python = env_root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    lock = ROOT / "requirements.lock"
    digest = hashlib.sha256(lock.read_bytes()).hexdigest()
    stamp = env_root / ".vesper-dependencies"
    if not python.exists():
        if args.no_install:
            raise SystemExit("The local environment is missing. Run again without --no-install.")
        print("Preparing this tool's private Python environment...", flush=True)
        venv.EnvBuilder(with_pip=True, symlinks=(os.name != "nt")).create(env_root)
    if not stamp.exists() or stamp.read_text().strip() != digest:
        if args.no_install:
            raise SystemExit("Local dependencies need preparation. Run again without --no-install.")
        print("Installing the pinned application packages from PyPI...", flush=True)
        subprocess.run(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--require-hashes",
                "-r",
                str(lock),
            ],
            check=True,
        )
        stamp.write_text(digest + "\n")
    if Path(sys.prefix).resolve() != env_root.resolve():
        result = subprocess.run([str(python), str(ROOT / "run.py"), *sys.argv[1:]], cwd=ROOT)
        raise SystemExit(result.returncode)
    web = importlib.import_module(PACKAGE + "." + MODULE)
    directory = (args.data_dir if args.data_dir is not None else web.default_data_dir()).expanduser().resolve()
    url = f"http://127.0.0.1:{args.port}"
    print(f"App folder: {ROOT}\nData directory: {directory}\nLocal address: {url}", flush=True)
    app = web.create_app(data_dir=directory, port=args.port, testing=args.testing)
    print(f"13-Week Cash Flow is opening at {url}. Keep this window open; press Ctrl+C to stop.", flush=True)
    if not args.no_browser:

        def open_when_ready():
            import urllib.request

            for _ in range(60):
                try:
                    with urllib.request.urlopen(url, timeout=0.5):
                        webbrowser.open(url)
                    return
                except (OSError, TimeoutError):
                    time.sleep(0.2)

        threading.Thread(target=open_when_ready, daemon=True).start()
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False, log_level="warning")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Local app stopped. Saved work is preserved.")
        raise SystemExit(0)
    except subprocess.CalledProcessError:
        raise SystemExit(
            "Package setup did not finish. Check the network connection and rerun; existing work is preserved."
        )

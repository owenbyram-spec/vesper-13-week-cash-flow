#!/usr/bin/env python3
"""Run the PE-native cash-flow workspace in separate local processes and data."""
from __future__ import annotations

import argparse
import errno
import hashlib
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import venv
import webbrowser

ROOT = Path(__file__).resolve().parent
FRONTEND = ROOT / "pe_platform" / "frontend"


def default_data_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/Vesper/pe-cash-flow"
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Vesper/pe-cash-flow"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "vesper/pe-cash-flow"


def free_port(port: int):
    if not 1024 <= port <= 65535:
        raise ValueError("Choose local ports from 1024 through 65535.")
    try:
        with socket.socket() as probe:
            if os.name != "nt":
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(("127.0.0.1", port))
    except OSError as error:
        if error.errno == errno.EADDRINUSE or getattr(error, "winerror", None) == 10048:
            raise ValueError(f"Port {port} is occupied. Choose another; no existing process was stopped.") from None
        raise ValueError(f"Local port {port} could not be opened (OS error {error.errno}). Check local listening permissions.") from None


def checked(arguments, *, cwd=ROOT, env=None):
    subprocess.run(arguments, cwd=cwd, env=env, check=True)


def clean_environment(extra=None):
    # No ambient database/provider/cloud credentials, proxy or dotenv paths reach the app.
    names = {"PATH", "LANG", "LC_ALL", "LC_CTYPE", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "TMPDIR"}
    result = {name: value for name, value in os.environ.items() if name in names}
    result.update({"PYTHONUTF8": "1", "PYTHON_DOTENV_DISABLED": "1",
                   "NEXT_TELEMETRY_DISABLED": "1", "AWS_EC2_METADATA_DISABLED": "true"})
    if extra:
        result.update(extra)
    return result


def browser_build_identity(frontend: Path, api_url: str) -> str:
    """Invalidate a prepared build when its source, lock or endpoint changes."""
    identity = hashlib.sha256(api_url.encode())
    roots = {"app", "components", "hooks", "lib", "providers", "public", "styles"}
    configurations = {
        "package.json", "package-lock.json", "tsconfig.json", "next-env.d.ts",
        "next.config.js", "next.config.mjs", "next.config.ts", "postcss.config.js",
        "postcss.config.mjs", "tailwind.config.js", "tailwind.config.ts",
    }
    files = [frontend / name for name in configurations if (frontend / name).is_file()]
    for name in roots:
        directory = frontend / name
        if directory.is_dir():
            files.extend(path for path in directory.rglob("*") if path.is_file())
    for path in sorted(files):
        relative = path.relative_to(frontend)
        identity.update(str(relative).replace(os.sep, "/").encode())
        identity.update(b"\0")
        identity.update(path.read_bytes())
    return identity.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8770, help="Local browser port")
    parser.add_argument("--api-port", type=int, help="Local API port (browser port + 1)")
    parser.add_argument("--data-dir", type=Path, default=default_data_dir())
    parser.add_argument("--no-install", action="store_true")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--backend-only", action="store_true")
    parser.add_argument("--testing", action="store_true", help="Isolated verification; memory credentials")
    args = parser.parse_args()
    children = []
    try:
        if sys.version_info < (3, 12):
            raise ValueError("Install Python 3.12 or newer.")
        api_port = args.api_port if args.api_port is not None else args.port + 1
        if api_port == args.port:
            raise ValueError("Choose different browser and API ports.")
        free_port(api_port)
        if not args.backend_only:
            free_port(args.port)
        directory = args.data_dir.expanduser().resolve()
        if (directory / "forecast.sqlite3").exists():
            raise ValueError("Choose a new platform data directory. Existing legacy cash-flow data is preserved separately.")
        browser_url, api_url = f"http://127.0.0.1:{args.port}", f"http://127.0.0.1:{api_port}"
        print(f"Source folder: {ROOT}\nPlatform data directory: {directory}\nBrowser: {browser_url}\nAPI: {api_url}", flush=True)
        from pe_platform.provenance import verify_source
        source = verify_source()
        print(f"PE source: {source['source_revision']} ({source['source_files_verified']} verified files)", flush=True)
        environment = ROOT / ".venv-platform"
        python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        lock = ROOT / "pe_platform/requirements.lock"
        if not lock.exists():
            raise ValueError("The pinned platform dependency lock is missing. Restore it from this repository.")
        digest = hashlib.sha256(lock.read_bytes()).hexdigest()
        stamp = environment / ".vesper-requirements"
        if not python.exists():
            if args.no_install:
                raise ValueError("The platform environment is missing. Run once without --no-install.")
            venv.EnvBuilder(with_pip=True, symlinks=os.name != "nt").create(environment)
        if not stamp.exists() or stamp.read_text().strip() != digest:
            if args.no_install:
                raise ValueError("The platform dependencies need preparation. Run without --no-install.")
            checked([str(python), "-m", "pip", "install", "--disable-pip-version-check", "--require-hashes", "-r", str(lock)])
            stamp.write_text(digest + "\n")
        node, npm = shutil.which("node"), shutil.which("npm")
        if not args.backend_only:
            if not node or not npm:
                raise ValueError("Install Node.js 20 or newer for the native browser workspace. The legacy CLI remains separate.")
            node_major = int(subprocess.check_output([node, "-p", "process.versions.node.split('.')[0]"], text=True).strip())
            if node_major < 20:
                raise ValueError("Use Node.js 20 or newer for the native workspace.")
            package_lock = FRONTEND / "package-lock.json"
            modules_stamp = FRONTEND / "node_modules/.vesper-lock"
            package_digest = hashlib.sha256(package_lock.read_bytes()).hexdigest()
            if not modules_stamp.exists() or modules_stamp.read_text().strip() != package_digest:
                if args.no_install:
                    raise ValueError("The browser packages need preparation. Run without --no-install.")
                checked([npm, "ci", "--ignore-scripts", "--no-audit", "--no-fund"], cwd=FRONTEND)
                modules_stamp.write_text(package_digest + "\n")
            build_environment = clean_environment({"NEXT_PUBLIC_BACKEND_URL": api_url, "BACKEND_URL": api_url,
                                                   "HOSTNAME": "127.0.0.1", "PORT": str(args.port)})
            build_stamp = FRONTEND / ".next/.vesper-origin"
            build_identity = browser_build_identity(FRONTEND, api_url)
            if not build_stamp.exists() or build_stamp.read_text().strip() != build_identity:
                if args.no_install:
                    raise ValueError("The native browser build needs preparation. Run without --no-install.")
                checked([npm, "run", "build"], cwd=FRONTEND, env=build_environment)
                build_stamp.write_text(build_identity + "\n")
        command = [str(python), "-m", "pe_platform.server", "--data-dir", str(directory),
                   "--port", str(api_port), "--frontend-origin", browser_url]
        if args.testing:
            command.append("--testing")
        children.append(subprocess.Popen(command, cwd=ROOT, env=clean_environment()))
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for _ in range(150):
            if children[0].poll() is not None:
                raise ValueError("The platform backend did not start; review its error above.")
            try:
                with opener.open(api_url + "/local/bootstrap", timeout=1) as response:
                    if response.status == 200:
                        break
            except (OSError, urllib.error.URLError):
                time.sleep(0.2)
        else:
            raise ValueError("The platform backend did not become ready.")
        if not args.backend_only:
            children.append(subprocess.Popen([node, ".next/standalone/server.js"], cwd=FRONTEND,
                                              env=clean_environment({"HOSTNAME": "127.0.0.1", "PORT": str(args.port),
                                                                     "BACKEND_URL": api_url, "NEXT_PUBLIC_BACKEND_URL": api_url})))
            if not args.no_browser:
                time.sleep(1)
                webbrowser.open(browser_url)
        print("Local PE cash-flow platform is running. Keep this terminal open; Ctrl+C stops only its child processes.", flush=True)
        while all(child.poll() is None for child in children):
            time.sleep(0.3)
        if any(child.returncode for child in children if child.returncode is not None):
            raise ValueError("A platform process exited; review the error above.")
        return 0
    except KeyboardInterrupt:
        print("Stopping this launcher's platform processes; saved data is retained.")
        return 0
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"Platform setup failed: {error}", file=sys.stderr)
        return 1
    finally:
        for child in reversed(children):
            if child.poll() is None:
                child.terminate()
        for child in reversed(children):
            try:
                child.wait(timeout=8)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()


if __name__ == "__main__":
    raise SystemExit(main())

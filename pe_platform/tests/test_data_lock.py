"""A second process must never open the same local finance directory."""

import subprocess
import sys
from pathlib import Path

from pe_platform.backend.data_lock import acquire_data_lock


ROOT = Path(__file__).resolve().parents[2]


def attempt(directory):
    return subprocess.run(
        [
            sys.executable,
            "-S",
            "-c",
            "from pathlib import Path; from pe_platform.backend.data_lock import acquire_data_lock; "
            "import sys; lease = acquire_data_lock(Path(sys.argv[1])); lease.close()",
            str(directory),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_data_lock_rejects_second_process_and_releases(tmp_path):
    lease = acquire_data_lock(tmp_path)
    try:
        blocked = attempt(tmp_path)
        assert blocked.returncode != 0
        assert "already open or cannot be locked" in blocked.stderr
        other = tmp_path / "separate"
        other.mkdir()
        assert attempt(other).returncode == 0
    finally:
        lease.close()
    assert attempt(tmp_path).returncode == 0

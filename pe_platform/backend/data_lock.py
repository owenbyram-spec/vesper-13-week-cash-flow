"""Hold one OS-backed writer lease for a local platform data directory."""

from __future__ import annotations

import atexit
import os
from pathlib import Path


class DataDirectoryLock:
    def __init__(self, directory: Path):
        self.path = directory / ".platform.lock"
        descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        self.handle = os.fdopen(descriptor, "r+b", buffering=0)
        try:
            if os.name == "nt":
                import msvcrt

                if self.path.stat().st_size == 0:
                    self.handle.write(b"\0")
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.handle.close()
            raise RuntimeError(
                "This platform data directory is already open or cannot be locked. "
                "Close its other platform instance or choose a separate data directory; "
                "no existing process was stopped."
            ) from None
        atexit.register(self.close)

    def close(self):
        if not self.handle.closed:
            # Closing the descriptor releases the OS lock, including after a crash.
            self.handle.close()
        atexit.unregister(self.close)


def acquire_data_lock(directory: Path) -> DataDirectoryLock:
    return DataDirectoryLock(directory)

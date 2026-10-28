"""Visible, opt-in, serial process-local polling. No operating-system scheduler."""

from __future__ import annotations

from datetime import datetime, timezone
import threading


def stamp():
    return datetime.now(timezone.utc).isoformat()


class PollingJob:
    def __init__(self, callback):
        self.callback = callback
        self.lock = threading.RLock()
        self.run_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread = None
        self.enabled = False
        self.interval_seconds = 300
        self.params = {}
        self.last_success = None
        self.last_error = None
        self.running = False
        self.workspace_id = None

    def status(self):
        with self.lock:
            return {
                "enabled": self.enabled,
                "running": self.running,
                "interval_seconds": self.interval_seconds,
                "workspace_id": self.workspace_id,
                "last_success": self.last_success,
                "last_error": self.last_error,
                "schedule": "Only while this app process is running and this workspace remains active.",
            }

    def run(self, params, workspace_id, callback=None):
        if not self.run_lock.acquire(blocking=False):
            raise ValueError("A refresh is already running. Wait for it to finish.")
        with self.lock:
            self.running = True
        try:
            result = (callback or self.callback)(dict(params), workspace_id)
            with self.lock:
                self.last_success, self.last_error = stamp(), None
            return result
        except Exception:
            with self.lock:
                self.last_error = {
                    "at": stamp(),
                    "message": "Refresh failed. Previous valid data is unchanged; review the connection and source controls.",
                }
            raise
        finally:
            with self.lock:
                self.running = False
            self.run_lock.release()

    def configure(self, *, enabled, interval_seconds=300, params=None, workspace_id):
        if type(interval_seconds) is not int or not 60 <= interval_seconds <= 86400:
            raise ValueError("Polling interval must be between 60 and 86400 seconds.")
        self.stop()
        with self.lock:
            self.enabled = enabled is True
            self.interval_seconds = interval_seconds
            self.params = dict(params or {})
            self.workspace_id = workspace_id
            if self.enabled:
                self.stop_event = threading.Event()
                self.thread = threading.Thread(target=self._loop, name="vesper-local-refresh", daemon=True)
                self.thread.start()
        return self.status()

    def _loop(self):
        event = self.stop_event
        while not event.wait(self.interval_seconds):
            try:
                self.run(self.params, self.workspace_id)
            except Exception:
                # Failure is visible in status. No output or provider error logging.
                pass

    def stop(self):
        with self.lock:
            self.enabled = False
            self.stop_event.set()
            thread = self.thread
            self.thread = None
        if thread and thread is not threading.current_thread():
            thread.join(timeout=0.1)

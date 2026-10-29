"""One bounded QBO review exchange with native terminal process custody.

The parent retains authenticated scope, retries, locks and financial decisions.
One libc-only C call owns every child/FD/spawn/I/O/wait transition until actual
terminal cleanup. No Python callback runs in that call. This source has not
qualified the pinned Linux runtime; the separately reviewed native artifact and
strict external public-return observations are required before admission.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
import ctypes
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import stat
import struct
import sys
import threading
import time
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlsplit

PROTOCOL = 2
MAGIC = b"BYRAMQH2"
REQUEST_MAX = 32768
REQUEST_PAYLOAD_MAX = REQUEST_MAX - 4
HEADER_MAX = 16384
CONTROL_PAYLOAD_MAX = HEADER_MAX - 5
DATA_MAX = 8388608
REFRESH_MAX = 524288
STDERR_MAX = 65536
FRAME_DATA_MAX = 65536
FRAME_PREFIX = struct.Struct("!cI")
READY_PAYLOAD = struct.Struct("!I32s32sIIQIQQQQQQQ")
PHYSICAL_NS = 30_000_000_000
FINISH_RESERVE_NS = 250_000_000
PARENT_RESERVE_NS = 50_000_000
ADDRESS_SPACE_MAX = 256 * 1024 * 1024
DESCRIPTORS_MAX = 32
TOKEN_ENDPOINT = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
REPORT_HOSTS = frozenset({"quickbooks.api.intuit.com", "sandbox-quickbooks.api.intuit.com"})
REPORT_PATH = re.compile(r"/v3/company/[0-9]{1,30}/reports/(GeneralLedger|TrialBalance|ProfitAndLoss|BalanceSheet)\Z")
NONCE_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
SOURCE_PATH = Path(__file__).resolve(strict=True)
_PACKAGED_NATIVE_PATH = SOURCE_PATH.with_name("_qbo_process_linux_amd64.so")
# Deployment packaging is separately scoped. No guessed or self-derived digest
# qualifies a production executable; an absent packaged admission fails closed.
_PACKAGED_NATIVE_SHA256: str | None = None
_NATIVE_TEST_PATH = Path("/native/libqbo_process.so")
_NATIVE_TEST_CONTROL = Path("/work/w2-native-artifact.json")
_WORKER_INTERPRETER = "/opt/venv/bin/python"
NATIVE_BYTES_MAX = 262144
NATIVE_ABI_MAGIC = 0x425952414D514832
NATIVE_ABI_VERSION = 2


@dataclass(frozen=True)
class CustodyReceipt:
    protocol: int
    nonce: str
    source_sha256: str
    parent_pid: int
    parent_start: int
    creator_tid: int
    creator_start: int
    original_deadline_ns: int
    physical_end_ns: int
    io_cutoff_ns: int
    cleanup_cutoff_ns: int
    invocation_ns: int
    pid: int | None = None
    child_start: int | None = None
    ready_ns: int | None = None
    native_terminal_ns: int | None = None
    # A Python checkpoint after parsing/callbacks, never external return proof.
    finished_ns: int | None = None
    request_bytes: int = 0
    request_bytes_written: int = 0
    credential_bytes_written: int = 0
    credentials_transferred: bool = False
    stdout_bytes: int = 0
    stdout_retained: int = 0
    stderr_bytes: int = 0
    response_bytes: int = 0
    stdout_eof: bool = False
    stderr_eof: bool = False
    raw_wait_status: int | None = None
    waited_pid: int | None = None
    raw_wait_consumed: bool = False
    returncode: int | None = None
    native_wait_calls: int = 0
    cancel_signal: int | None = None
    cancel_calls: int = 0
    constructor_returned: bool = False
    constructor_ambiguous: bool = False
    custody_complete: bool = False
    terminal_cleanup_complete: bool = False
    deadline_met: bool = False
    failure: str | None = None
    native_failure: int = 0
    native_stage: int = 0
    native_errno: int = 0
    spawn_rc: int = 0
    wait_loss: bool = False
    ready_verified: bool = False
    owned_fd_count: int = 0
    fds_closed: bool = False
    closed_fd_count: int = 0
    max_owned_fd_count: int = 0
    stdin_closed: bool = False
    pidfd_acquired: bool = False
    pidfd_terminal: bool = False
    protocol_complete: bool = False
    protocol_failure_frame: bool = False
    frame_count: int = 0
    data_frame_count: int = 0
    read_calls: int = 0
    write_calls: int = 0
    poll_calls: int = 0
    native_phase: int = 0
    launch_auxiliary_unobserved: bool = False
    native_artifact_sha256: str | None = None
    native_abi_sha256: str | None = None


@dataclass(frozen=True)
class Snapshot:
    status_code: int
    headers: tuple[tuple[str, str], ...]
    content: bytes
    receipt: CustodyReceipt


class _ExchangeError(Exception):
    """Only fixed codes and sanitized metadata, never provider exception text."""
    def __init__(self, code: str, receipt: CustodyReceipt | None = None):
        super().__init__(code)
        self.code, self.receipt = code, receipt


class DeadlineExceeded(_ExchangeError):
    pass


class TransportRefused(_ExchangeError):
    pass


class CustodyFailure(_ExchangeError):
    pass


def _canonical(document: Any) -> bytes:
    return json.dumps(document, ensure_ascii=True, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("ascii")


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise TransportRefused("duplicate_protocol_key")
        result[key] = value
    return result


def _nonfinite(_: str) -> None:
    raise TransportRefused("nonfinite_protocol_number")


def _decode_json(raw: bytes, maximum: int) -> Any:
    if type(raw) is not bytes or len(raw) > maximum:
        raise TransportRefused("protocol_bytes_exhausted")
    # All our frames are shallow; reject nesting before the recursive decoder.
    depth, quoted, escaped = 0, False, False
    for char in raw:
        if quoted:
            if escaped:
                escaped = False
            elif char == 92:
                escaped = True
            elif char == 34:
                quoted = False
        elif char == 34:
            quoted = True
        elif char in (91, 123):
            depth += 1
            if depth > 8:
                raise TransportRefused("protocol_depth_exhausted")
        elif char in (93, 125):
            depth -= 1
            if depth < 0:
                raise TransportRefused("invalid_protocol_json")
    try:
        return json.loads(raw.decode("ascii"), object_pairs_hook=_unique_pairs,
                          parse_constant=_nonfinite)
    except (ValueError, UnicodeError, RecursionError):
        raise TransportRefused("invalid_protocol_json") from None


def _source_hash() -> str:
    """Bind the exact regular product file without following an exchanged link."""
    descriptor = os.open(SOURCE_PATH, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        import stat
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > 256 * 1024:
            raise TransportRefused("product_source_identity_unavailable")
        digest, count = hashlib.sha256(), 0
        while True:
            block = os.read(descriptor, 65536)
            if not block:
                break
            count += len(block)
            if count > 256 * 1024:
                raise TransportRefused("product_source_identity_unavailable")
            digest.update(block)
        after = os.fstat(descriptor)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) or count != before.st_size:
            raise TransportRefused("product_source_identity_changed")
        return digest.hexdigest()
    finally:
        os.close(descriptor)


def _identity(pid: int, *, tid: int | None = None) -> tuple[int, int, int, int]:
    """Actual Linux (pid-or-tid, parent PID, process group, start ticks)."""
    if type(pid) is not int or pid <= 0 or (tid is not None and (type(tid) is not int or tid <= 0)):
        raise TransportRefused("invalid_process_identity")
    path = f"/proc/{pid}/stat" if tid is None else f"/proc/{pid}/task/{tid}/stat"
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            raw = os.read(descriptor, 8193)
        finally:
            os.close(descriptor)
        end = raw.rfind(b") ")
        if end < 0 or len(raw) > 8192:
            raise ValueError
        fields = raw[end + 2:].split()
        actual = int(raw[:raw.find(b" (")])
        result = actual, int(fields[1]), int(fields[2]), int(fields[19])
        if result[0] != (pid if tid is None else tid) or result[3] <= 0:
            raise ValueError
        return result
    except (OSError, ValueError, IndexError):
        raise TransportRefused("process_identity_unavailable") from None


def _platform_guard() -> None:
    if (sys.platform != "linux" or sys.version_info[:3] != (3, 12, 3)
            or not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal")
            or not hasattr(signal, "pthread_sigmask") or not hasattr(threading, "get_native_id")
            or os.uname().machine != "x86_64"):
        raise TransportRefused("unqualified_http_runtime")


def _worker_environment() -> dict[str, str]:
    # No inherited HOME, proxy, Python, cloud, DB, application or token settings.
    return {"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}


def _text(value: Any, maximum: int) -> str:
    if (not isinstance(value, str) or not value or len(value) > maximum
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise TransportRefused("invalid_request_text")
    return value


def _pairs(value: Any) -> dict[str, str]:
    if isinstance(value, Mapping):
        items = list(value.items())
    elif isinstance(value, (tuple, list)):
        items = list(value)
    else:
        raise TransportRefused("invalid_request_mapping")
    if len(items) > 8:
        raise TransportRefused("invalid_request_mapping")
    result: dict[str, str] = {}
    try:
        for item in items:
            if not isinstance(item, (tuple, list)) or len(item) != 2:
                raise TransportRefused("invalid_request_mapping")
            key, val = item
            key, val = _text(key, 64), _text(val, 16384)
            if key in result:
                raise TransportRefused("duplicate_request_key")
            result[key] = val
    except (TypeError, ValueError):
        raise TransportRefused("invalid_request_mapping") from None
    return result


def _validate_intent(method: Any, endpoint: Any, headers: Any, params: Any,
                     data: Any, cap: Any) -> dict[str, Any]:
    if type(cap) is not int or cap <= 0 or cap > DATA_MAX:
        raise TransportRefused("invalid_response_ceiling")
    if (not isinstance(endpoint, str) or len(endpoint) > 256
            or type(method) is not str or method not in {"GET", "POST"}):
        raise TransportRefused("unapproved_http_intent")
    try:
        parsed = urlsplit(endpoint)
        port = parsed.port
    except ValueError:
        raise TransportRefused("unapproved_http_endpoint") from None
    if (parsed.scheme != "https" or parsed.query or parsed.fragment or parsed.username is not None
            or parsed.password is not None or port is not None or parsed.netloc != parsed.hostname):
        raise TransportRefused("unapproved_http_endpoint")
    supplied_headers = _pairs(headers)
    normalized: dict[str, str] = {}
    for key, value in supplied_headers.items():
        lowered = key.lower()
        if lowered in normalized:
            raise TransportRefused("duplicate_request_header")
        normalized[lowered] = value
    allowed = {"authorization", "accept", "accept-encoding"}
    if method == "POST":
        allowed.add("content-type")
    if (set(normalized) - allowed or normalized.get("accept") != "application/json"
            or normalized.get("accept-encoding", "identity").lower() != "identity"):
        raise TransportRefused("unapproved_request_headers")
    normalized["accept-encoding"] = "identity"
    if method == "GET":
        if (parsed.hostname not in REPORT_HOSTS or REPORT_PATH.fullmatch(parsed.path) is None
                or not normalized.get("authorization", "").startswith("Bearer ") or data is not None):
            raise TransportRefused("unapproved_report_intent")
        selected = _pairs(params)
        if (set(selected) != {"minorversion", "start_date", "end_date", "accounting_method"}
                or selected["minorversion"] != "75" or selected["accounting_method"] not in {"Cash", "Accrual"}):
            raise TransportRefused("unapproved_report_query")
        try:
            start, end = date.fromisoformat(selected["start_date"]), date.fromisoformat(selected["end_date"])
        except ValueError:
            raise TransportRefused("unapproved_report_query") from None
        if (start.isoformat() != selected["start_date"] or end.isoformat() != selected["end_date"]
                or start > end):
            raise TransportRefused("unapproved_report_query")
        form = None
    else:
        if (endpoint != TOKEN_ENDPOINT or params is not None or cap > REFRESH_MAX
                or not normalized.get("authorization", "").startswith("Basic ")
                or normalized.get("content-type") != "application/x-www-form-urlencoded"):
            raise TransportRefused("unapproved_refresh_intent")
        form = _pairs(data)
        if set(form) != {"grant_type", "refresh_token"} or form["grant_type"] != "refresh_token":
            raise TransportRefused("unapproved_refresh_form")
        selected = None
    return {"method": method, "endpoint": endpoint, "headers": normalized,
            "params": selected, "data": form, "cap": cap}


@dataclass(frozen=True)
class _Launch:
    nonce: str
    parent_pid: int
    parent_start: int
    creator_tid: int
    creator_start: int
    original_deadline_ns: int
    physical_end_ns: int
    io_cutoff_ns: int
    cleanup_cutoff_ns: int
    source_sha256: str
    invocation_ns: int

    def arguments(self) -> list[str]:
        return [self.nonce, str(self.parent_pid), str(self.parent_start), str(self.creator_tid),
                str(self.creator_start), str(self.original_deadline_ns), str(self.physical_end_ns),
                str(self.io_cutoff_ns), str(self.cleanup_cutoff_ns), self.source_sha256,
                str(self.invocation_ns)]

    def receipt(self) -> CustodyReceipt:
        return CustodyReceipt(PROTOCOL, self.nonce, self.source_sha256, self.parent_pid,
                              self.parent_start, self.creator_tid, self.creator_start,
                              self.original_deadline_ns, self.physical_end_ns, self.io_cutoff_ns,
                              self.cleanup_cutoff_ns, self.invocation_ns)


def _worker_argv(launch: _Launch) -> list[str]:
    """The sole private native-fixture seam; production accepts no worker path."""
    # Preserve an absolute virtualenv interpreter path: resolving its symlink
    # would discard the pinned virtualenv's site-packages selection.
    interpreter = _WORKER_INTERPRETER
    if sys.executable != interpreter or not os.path.isabs(interpreter):
        raise TransportRefused("http_interpreter_unavailable")
    return [interpreter, "-I", "-B", str(SOURCE_PATH), "--worker", *launch.arguments()]


def _launch_executable(launch: _Launch, argv: Sequence[str]) -> str:
    """Closed test bootstrap may replace this together with _worker_argv only."""
    return _WORKER_INTERPRETER


def _launch_metadata(budget: Any) -> _Launch:
    _platform_guard()
    budget.remaining()
    original = budget.deadline
    if (not isinstance(original, (int, float)) or isinstance(original, bool)
            or not math.isfinite(original)):
        raise TransportRefused("invalid_original_deadline")
    invocation = time.monotonic_ns()
    original_ns = int(original * 1_000_000_000)
    physical = min(original_ns, invocation + PHYSICAL_NS)
    if physical - invocation <= FINISH_RESERVE_NS:
        raise DeadlineExceeded("http_envelope_unavailable")
    parent, tid = os.getpid(), threading.get_native_id()
    if threading.current_thread() is not threading.main_thread() or parent != tid:
        raise TransportRefused("unsupported_http_supervisor_thread")
    parent_identity, thread_identity = _identity(parent), _identity(parent, tid=tid)
    source = _source_hash()
    launch = _Launch(os.urandom(32).hex(), parent, parent_identity[3], tid, thread_identity[3],
                     original_ns, physical, physical - FINISH_RESERVE_NS,
                     physical - PARENT_RESERVE_NS, source, invocation)
    if time.monotonic_ns() >= launch.io_cutoff_ns:
        raise DeadlineExceeded("http_envelope_unavailable", launch.receipt())
    return launch


def _frame(kind: bytes, body: bytes) -> bytes:
    if kind not in {b"R", b"H", b"D", b"E", b"F"} or type(body) is not bytes:
        raise TransportRefused("invalid_protocol_frame")
    maximum = FRAME_DATA_MAX if kind == b"D" else CONTROL_PAYLOAD_MAX
    if (kind == b"R" and len(body) != READY_PAYLOAD.size) or len(body) > maximum:
        raise TransportRefused("protocol_bytes_exhausted")
    return FRAME_PREFIX.pack(kind, len(body)) + body



def _ready_payload(launch: _Launch, child: tuple[int, int, int, int] | None = None) -> bytes:
    """Fixed binary READY; native code verifies independent held-child identity."""
    actual = _identity(os.getpid()) if child is None else child
    return READY_PAYLOAD.pack(PROTOCOL, bytes.fromhex(launch.nonce),
        bytes.fromhex(launch.source_sha256), actual[0], launch.parent_pid,
        launch.parent_start, launch.creator_tid, launch.creator_start, actual[3],
        launch.original_deadline_ns, launch.physical_end_ns, launch.io_cutoff_ns,
        launch.cleanup_cutoff_ns, launch.invocation_ns)


def _stdout_capacity(cap: int) -> int:
    return cap + 32925 + 5 * ((cap + FRAME_DATA_MAX - 1) // FRAME_DATA_MAX)


class _ResponseFrames:
    """Bounded semantic decoder, used only after native terminal custody."""
    def __init__(self, launch: _Launch, cap: int):
        self.launch, self.cap = launch, cap
        self.buffer = bytearray()
        self.magic = False
        self.ready: dict[str, Any] | None = None
        self.status: int | None = None
        self.headers: tuple[tuple[str, str], ...] = ()
        self.content = bytearray()
        self.end = False
        self.refusal: str | None = None
        self.frame_count = self.data_frame_count = self.control_count = 0
        self.observed = 0
        self.short_data = False

    def feed(self, block: bytes) -> None:
        if type(block) is not bytes:
            raise TransportRefused("invalid_protocol_bytes")
        self.observed += len(block)
        if self.observed > _stdout_capacity(self.cap):
            raise TransportRefused("protocol_bytes_exhausted")
        self.buffer.extend(block)
        if not self.magic:
            if len(self.buffer) < len(MAGIC):
                return
            if bytes(self.buffer[:len(MAGIC)]) != MAGIC:
                raise TransportRefused("invalid_protocol_magic")
            del self.buffer[:len(MAGIC)]
            self.magic = True
        while len(self.buffer) >= FRAME_PREFIX.size:
            kind, length = FRAME_PREFIX.unpack(self.buffer[:FRAME_PREFIX.size])
            maximum = FRAME_DATA_MAX if kind == b"D" else CONTROL_PAYLOAD_MAX
            if (kind not in {b"R", b"H", b"D", b"E", b"F"} or length > maximum
                    or (kind == b"R" and length != READY_PAYLOAD.size)):
                raise TransportRefused("invalid_protocol_frame")
            if len(self.buffer) < FRAME_PREFIX.size + length:
                break
            body = bytes(self.buffer[FRAME_PREFIX.size:FRAME_PREFIX.size + length])
            del self.buffer[:FRAME_PREFIX.size + length]
            self._accept(kind, body)
        if len(self.buffer) > FRAME_PREFIX.size + FRAME_DATA_MAX:
            raise TransportRefused("protocol_bytes_exhausted")

    def _accept(self, kind: bytes, body: bytes) -> None:
        self.frame_count += 1
        if (self.frame_count > 3 + (self.cap + FRAME_DATA_MAX - 1) // FRAME_DATA_MAX
                or self.end or self.refusal is not None):
            raise TransportRefused("unexpected_protocol_frame")
        if self.ready is None:
            if kind != b"R" or len(body) != READY_PAYLOAD.size:
                raise TransportRefused("ready_required")
            fields = READY_PAYLOAD.unpack(body)
            keys = ("protocol", "nonce", "source_sha256", "pid", "parent_pid", "parent_start",
                    "creator_tid", "creator_start", "child_start", "original_deadline_ns",
                    "physical_end_ns", "io_cutoff_ns", "cleanup_cutoff_ns", "invocation_ns")
            ready = dict(zip(keys, fields))
            ready["nonce"], ready["source_sha256"] = ready["nonce"].hex(), ready["source_sha256"].hex()
            expected = {"protocol": PROTOCOL, "nonce": self.launch.nonce,
                "source_sha256": self.launch.source_sha256, "parent_pid": self.launch.parent_pid,
                "parent_start": self.launch.parent_start, "creator_tid": self.launch.creator_tid,
                "creator_start": self.launch.creator_start, "original_deadline_ns": self.launch.original_deadline_ns,
                "physical_end_ns": self.launch.physical_end_ns, "io_cutoff_ns": self.launch.io_cutoff_ns,
                "cleanup_cutoff_ns": self.launch.cleanup_cutoff_ns, "invocation_ns": self.launch.invocation_ns}
            if any(ready.get(key) != value for key, value in expected.items()):
                raise TransportRefused("ready_identity_mismatch")
            if ready["pid"] <= 0 or ready["child_start"] <= 0:
                raise TransportRefused("invalid_ready_identity")
            self.ready = ready
            return
        if kind in {b"H", b"E", b"F"}:
            self.control_count += 1
            if self.control_count > 2:
                raise TransportRefused("unexpected_protocol_control")
        if kind == b"F":
            refusal = _decode_json(body, CONTROL_PAYLOAD_MAX)
            if (not isinstance(refusal, dict) or set(refusal) != {"code", "nonce"}
                    or refusal["nonce"] != self.launch.nonce or refusal["code"] not in {
                        "transport_failed", "response_bytes_exhausted", "response_encoding_refused",
                        "response_headers_exhausted", "invalid_request_frame", "worker_resource_refused",
                        "worker_deadline_expired"}):
                raise TransportRefused("invalid_refusal_frame")
            self.refusal = refusal["code"]
            return
        if self.status is None:
            if kind != b"H":
                raise TransportRefused("response_headers_required")
            header = _decode_json(body, CONTROL_PAYLOAD_MAX)
            if (not isinstance(header, dict) or set(header) != {"nonce", "status", "headers"}
                    or header["nonce"] != self.launch.nonce or type(header["status"]) is not int
                    or not 100 <= header["status"] <= 599 or not isinstance(header["headers"], list)
                    or len(header["headers"]) > 128):
                raise TransportRefused("invalid_response_headers")
            pairs = []
            for pair in header["headers"]:
                if not isinstance(pair, list) or len(pair) != 2:
                    raise TransportRefused("invalid_response_headers")
                key, value = pair
                if (not isinstance(key, str) or not isinstance(value, str) or not key
                        or len(key) > 256 or len(value) > CONTROL_PAYLOAD_MAX
                        or any(ord(char) < 32 or ord(char) == 127 for char in key + value)):
                    raise TransportRefused("invalid_response_headers")
                pairs.append((key, value))
            if any(key.lower() == "content-encoding" and value.strip().lower() not in {"", "identity"}
                   for key, value in pairs):
                raise TransportRefused("response_encoding_refused")
            self.status, self.headers = header["status"], tuple(pairs)
        elif kind == b"D":
            self.data_frame_count += 1
            if (not body or self.short_data or len(self.content) + len(body) > self.cap
                    or self.data_frame_count > (self.cap + FRAME_DATA_MAX - 1) // FRAME_DATA_MAX):
                raise TransportRefused("response_bytes_exhausted")
            self.short_data = len(body) != FRAME_DATA_MAX
            self.content.extend(body)
        elif kind == b"E":
            end = _decode_json(body, CONTROL_PAYLOAD_MAX)
            if (not isinstance(end, dict) or set(end) != {"nonce", "bytes", "sha256"}
                    or end["nonce"] != self.launch.nonce or type(end["bytes"]) is not int
                    or end["bytes"] != len(self.content)
                    or end["sha256"] != hashlib.sha256(self.content).hexdigest()):
                raise TransportRefused("response_end_mismatch")
            self.end = True
        else:
            raise TransportRefused("unexpected_protocol_frame")

    def complete(self) -> bool:
        return self.end and not self.buffer and self.refusal is None


# Exact fixed-width v2 layout; the side-effect-free ABI query checks every field
# offset and size before any exchange may acquire a pipe or spawn a child.
_U32, _U64, _BYTES32 = ctypes.c_uint32, ctypes.c_uint64, ctypes.c_uint8 * 32
_INPUT_FIELDS = [
    ("abi_version", _U32), ("struct_bytes", _U32), ("parent_pid", _U32), ("creator_tid", _U32),
    ("argument_count", _U32), ("environment_count", _U32), ("request_bytes", _U32), ("body_cap", _U32),
    ("stdout_capacity", _U32), ("reserved0", _U32),
    ("parent_start", _U64), ("creator_start", _U64), ("original_deadline_ns", _U64), ("physical_end_ns", _U64),
    ("io_cutoff_ns", _U64), ("cleanup_cutoff_ns", _U64), ("invocation_ns", _U64),
    ("executable_addr", _U64), ("argv_addr", _U64), ("envp_addr", _U64), ("request_addr", _U64), ("stdout_addr", _U64),
    ("nonce", _BYTES32), ("source_sha256", _BYTES32)]
_STATE_FIELDS = [(name, _U32) for name in (
    "abi_version", "struct_bytes", "phase", "failure", "stage", "syscall_errno", "spawn_rc",
    "child_pid", "waited_pid", "raw_wait_status", "raw_wait_consumed", "wait_loss", "ready_verified",
    "stdout_eof", "stderr_eof", "stdin_closed", "owned_fd_count", "fds_closed", "spawned", "pidfd_acquired",
    "cancel_signal", "cancel_calls", "protocol_complete", "protocol_failure_frame", "exit_success", "deadline_met",
    "launch_auxiliary_unobserved", "closed_fd_count", "max_owned_fd_count", "pidfd_terminal")]
_STATE_FIELDS += [(name, _U64) for name in (
    "child_start", "ready_ns", "native_terminal_ns", "request_bytes_written", "stdout_bytes_observed",
    "stderr_bytes_observed", "stdout_bytes_retained", "body_bytes", "frame_count", "data_frame_count",
    "wait_calls", "write_calls", "read_calls", "poll_calls", "credential_bytes_written")]


class _NativeInput(ctypes.Structure):
    _fields_ = _INPUT_FIELDS


class _NativeState(ctypes.Structure):
    _fields_ = _STATE_FIELDS


def _expected_abi() -> tuple[int, ...]:
    words = [NATIVE_ABI_MAGIC, NATIVE_ABI_VERSION, ctypes.sizeof(_NativeInput), ctypes.alignment(_NativeInput),
             ctypes.sizeof(_NativeState), ctypes.alignment(_NativeState), len(_INPUT_FIELDS), len(_STATE_FIELDS)]
    for structure, fields in ((_NativeInput, _INPUT_FIELDS), (_NativeState, _STATE_FIELDS)):
        for name, field_type in fields:
            words.extend((getattr(structure, name).offset, ctypes.sizeof(field_type)))
    return tuple(words)


class _NativeLibrary:
    def __init__(self, owner: Any, library: Any, digest: str):
        self.owner, self.library, self.sha256 = owner, library, digest
        self.abi = library.byram_qbo_process_abi
        self.abi.argtypes, self.abi.restype = [ctypes.POINTER(_U64), _U32], None
        self.exchange = library.byram_qbo_process_exchange
        self.exchange.argtypes, self.exchange.restype = [ctypes.POINTER(_NativeInput), ctypes.POINTER(_NativeState)], None
        self.phase = library.byram_qbo_process_phase
        self.phase.argtypes, self.phase.restype = [ctypes.POINTER(_NativeState)], _U32
        expected = _expected_abi()
        descriptor = (_U64 * len(expected))()
        self.abi(descriptor, len(expected))
        if tuple(descriptor) != expected or ctypes.sizeof(_NativeState) > 4096 or ctypes.sizeof(ctypes.c_void_p) != 8:
            raise TransportRefused("native_abi_mismatch")
        self.descriptor = descriptor
        self.abi_sha256 = hashlib.sha256(struct.pack("!" + "Q" * len(expected), *expected)).hexdigest()
        self.active_call: _NativeCall | None = None


_NATIVE_ATTEMPTED = False
_NATIVE_DISABLED = True
_NATIVE_OWNER: Any = None
_NATIVE_LIBRARY: _NativeLibrary | None = None
_NATIVE_CDLL: Any = None
_NATIVE_LOAD_LOCK = threading.Lock()


def _file_identity(value: Any) -> tuple[int, ...]:
    # Reads may legitimately advance atime; it is not an executable identity.
    return (value.st_dev, value.st_ino, value.st_mode, value.st_uid, value.st_gid,
            value.st_nlink, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def _native_identity(owner: Any) -> tuple[tuple[int, ...], str]:
    before = os.fstat(owner.fileno())
    if (not stat.S_ISREG(before.st_mode) or stat.S_IMODE(before.st_mode) != 0o500
            or before.st_uid != os.geteuid() or before.st_nlink != 1
            or not 0 < before.st_size <= NATIVE_BYTES_MAX):
        raise TransportRefused("native_artifact_identity_refused")
    owner.seek(0)
    digest, count = hashlib.sha256(), 0
    while True:
        block = owner.read(65536)
        if not block:
            break
        count += len(block)
        if count > NATIVE_BYTES_MAX:
            raise TransportRefused("native_artifact_bytes_exhausted")
        digest.update(block)
    after = os.fstat(owner.fileno())
    if _file_identity(before) != _file_identity(after) or count != before.st_size:
        raise TransportRefused("native_artifact_identity_changed")
    return _file_identity(after), digest.hexdigest()


def _load_native(path: Path, expected_sha256: str | None) -> _NativeLibrary:
    global _NATIVE_ATTEMPTED, _NATIVE_DISABLED, _NATIVE_OWNER, _NATIVE_CDLL, _NATIVE_LIBRARY
    _platform_guard()
    # One process-local attempt is pre-latched even if Python control interrupts
    # opening/loading/ABI query. Retain owner/CDLL references for process lifetime.
    with _NATIVE_LOAD_LOCK:
        if _NATIVE_ATTEMPTED:
            if (_NATIVE_DISABLED or _NATIVE_LIBRARY is None
                    or _NATIVE_LIBRARY.sha256 != expected_sha256
                    or Path(_NATIVE_LIBRARY.owner.name) != path):
                raise TransportRefused("native_artifact_disabled")
            return _NATIVE_LIBRARY
        _NATIVE_ATTEMPTED, _NATIVE_DISABLED = True, True
        if (not isinstance(expected_sha256, str) or NONCE_PATTERN.fullmatch(expected_sha256) is None
                or path not in {_PACKAGED_NATIVE_PATH, _NATIVE_TEST_PATH} or not path.is_absolute()):
            raise TransportRefused("native_artifact_unqualified")
        try:
            _NATIVE_OWNER = open(path, "rb", buffering=0,
                opener=lambda selected, flags: os.open(selected, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC))
            identity, digest = _native_identity(_NATIVE_OWNER)
            if digest != expected_sha256:
                raise TransportRefused("native_artifact_digest_mismatch")
            _NATIVE_CDLL = ctypes.CDLL(f"/proc/self/fd/{_NATIVE_OWNER.fileno()}",
                mode=os.RTLD_NOW | os.RTLD_LOCAL)
            native = _NativeLibrary(_NATIVE_OWNER, _NATIVE_CDLL, digest)
            after_identity, after_digest = _native_identity(_NATIVE_OWNER)
            if identity != after_identity or digest != after_digest:
                raise TransportRefused("native_artifact_changed_during_load")
            _NATIVE_LIBRARY = native
            _NATIVE_DISABLED = False
            return native
        except _ExchangeError:
            raise
        except Exception:
            raise TransportRefused("native_artifact_load_refused") from None


def _native_factory() -> _NativeLibrary:
    return _load_native(_PACKAGED_NATIVE_PATH, _PACKAGED_NATIVE_SHA256)


def _native_test_factory(admitted_sha256: str) -> _NativeLibrary:
    """Private fixed path used only by the reviewed closed Linux bootstrap."""
    if (SOURCE_PATH != Path("/src/current/app/accounting_sources/qbo_http.py")
            or os.environ.get("ACCOUNTING_SOURCE_W2_RUNTIME_ADMITTED") != "1"
            or NONCE_PATTERN.fullmatch(os.environ.get("W2_RUN_NONCE", "")) is None):
        raise TransportRefused("native_test_admission_missing")
    try:
        with open(_NATIVE_TEST_CONTROL, "rb", buffering=0,
                opener=lambda selected, flags: os.open(selected, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)) as control:
            identity = os.fstat(control.fileno())
            if (not stat.S_ISREG(identity.st_mode) or identity.st_uid != os.geteuid()
                    or stat.S_IMODE(identity.st_mode) != 0o600 or identity.st_nlink != 1
                    or not 0 < identity.st_size <= 2048):
                raise TransportRefused("native_test_control_refused")
            raw = control.read(2049)
            after = os.fstat(control.fileno())
            if len(raw) != identity.st_size or _file_identity(after) != _file_identity(identity):
                raise TransportRefused("native_test_control_changed")
        document = _decode_json(raw, 2048)
        if (not isinstance(document, dict) or set(document) != {
                "schema_version", "job_nonce", "source_sha256", "elf_sha256", "path"}
                or document["schema_version"] != "w2-native-artifact/v1"
                or document["job_nonce"] != os.environ["W2_RUN_NONCE"]
                or document["elf_sha256"] != admitted_sha256
                or document["path"] != str(_NATIVE_TEST_PATH)):
            raise TransportRefused("native_test_control_refused")
        c_source = SOURCE_PATH.with_name("qbo_process.c")
        with open(c_source, "rb", buffering=0,
                opener=lambda selected, flags: os.open(selected, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)) as source:
            identity = os.fstat(source.fileno())
            if (not stat.S_ISREG(identity.st_mode) or identity.st_nlink != 1
                    or not 0 < identity.st_size <= 65536):
                raise TransportRefused("native_test_source_refused")
            raw = source.read(65537)
            after = os.fstat(source.fileno())
            if (len(raw) != identity.st_size or _file_identity(after) != _file_identity(identity)
                    or hashlib.sha256(raw).hexdigest() != document["source_sha256"]):
                raise TransportRefused("native_test_source_changed")
    except _ExchangeError:
        raise
    except Exception:
        raise TransportRefused("native_test_control_refused") from None
    return _load_native(_NATIVE_TEST_PATH, admitted_sha256)


class _NativeCall:
    """All immutable inputs/writable outputs are retained before native entry."""
    def __init__(self, native: _NativeLibrary, launch: _Launch, request: bytes, cap: int):
        if (type(request) is not bytes or not 0 < len(request) <= REQUEST_PAYLOAD_MAX
                or type(cap) is not int or not 0 < cap <= DATA_MAX):
            raise TransportRefused("invalid_native_exchange_input")
        self.native, self.launch, self.cap = native, launch, cap
        argv = _worker_argv(launch)
        executable = _launch_executable(launch, argv)
        if (not isinstance(argv, list) or not 3 <= len(argv) <= 32
                or type(executable) is not str or not os.path.isabs(executable)
                or not argv or argv[0] != executable
                or any(type(value) is not str or not value or "\x00" in value or len(value.encode()) > 4096 for value in argv)
                or sum(len(value.encode()) + 1 for value in argv) > 16384):
            raise TransportRefused("invalid_native_worker_arguments")
        self.argv_bytes = tuple(value.encode("utf-8") for value in argv)
        self.environment_bytes = (b"LANG=C.UTF-8", b"LC_ALL=C.UTF-8")
        self.argv = (ctypes.c_char_p * (len(self.argv_bytes) + 1))(*self.argv_bytes, None)
        self.environment = (ctypes.c_char_p * 3)(*self.environment_bytes, None)
        self.executable = ctypes.create_string_buffer(executable.encode("utf-8"))
        framed = struct.pack("!I", len(request)) + request
        self.request = ctypes.create_string_buffer(framed, len(framed))
        self.output = (ctypes.c_uint8 * _stdout_capacity(cap))()
        self.state, self.input = _NativeState(), _NativeInput()
        self.state.abi_version, self.state.struct_bytes = NATIVE_ABI_VERSION, ctypes.sizeof(_NativeState)
        self.input.abi_version, self.input.struct_bytes = NATIVE_ABI_VERSION, ctypes.sizeof(_NativeInput)
        for name in ("parent_pid", "creator_tid", "parent_start", "creator_start", "original_deadline_ns",
                     "physical_end_ns", "io_cutoff_ns", "cleanup_cutoff_ns", "invocation_ns"):
            setattr(self.input, name, getattr(launch, name))
        self.input.argument_count, self.input.environment_count = len(self.argv_bytes), 2
        self.input.request_bytes, self.input.body_cap = len(framed), cap
        self.input.stdout_capacity = len(self.output)
        for name, buffer in (("executable_addr", self.executable), ("argv_addr", self.argv),
                             ("envp_addr", self.environment), ("request_addr", self.request), ("stdout_addr", self.output)):
            setattr(self.input, name, ctypes.addressof(buffer))
        self.input.nonce = _BYTES32.from_buffer_copy(bytes.fromhex(launch.nonce))
        self.input.source_sha256 = _BYTES32.from_buffer_copy(bytes.fromhex(launch.source_sha256))
        self._input_arg, self._state_arg = ctypes.byref(self.input), ctypes.byref(self.state)
        self.entered = False

    def invoke(self) -> None:
        if self.entered or self.native.active_call is not None:
            raise CustodyFailure("native_exchange_reentry_refused")
        self.native.active_call = self
        self.entered = True
        # This is the sole child-owning boundary. It returns void. No callback,
        # errcheck, Python signal check or Python scalar result carries custody.
        self.native.exchange(self._input_arg, self._state_arg)

    def raw_stdout(self) -> bytes:
        if self.state.phase != 10 or self.state.owned_fd_count or not self.state.fds_closed:
            raise CustodyFailure("native_terminal_state_missing")
        retained = int(self.state.stdout_bytes_retained)
        if not 0 <= retained <= len(self.output):
            raise CustodyFailure("native_output_extent_invalid")
        return bytes(memoryview(self.output).cast("B")[:retained])


def _receipt_from_state(call: _NativeCall) -> CustodyReceipt:
    state, launch = call.state, call.launch
    if (state.abi_version != NATIVE_ABI_VERSION or state.struct_bytes != ctypes.sizeof(_NativeState)
            or state.phase != 10 or state.owned_fd_count or not state.fds_closed):
        raise CustodyFailure("native_terminal_state_missing", launch.receipt())
    consumed = bool(state.raw_wait_consumed)
    raw = int(state.raw_wait_status) if consumed else None
    waited = int(state.waited_pid) if consumed else None
    terminal = bool(state.native_terminal_ns and state.fds_closed and state.owned_fd_count == 0)
    raw_terminal = bool(consumed and not state.wait_loss and waited == state.child_pid and state.wait_calls >= 1)
    loss_terminal = bool(state.wait_loss and state.pidfd_acquired and state.pidfd_terminal)
    admitted_cleanup = bool(terminal and state.stdin_closed and state.stdout_eof and state.stderr_eof
        and state.spawned and state.child_pid and (raw_terminal or loss_terminal))
    no_child_cleanup = bool(terminal and not state.spawned and not state.child_pid
        and not consumed and not state.request_bytes_written and not state.ready_verified)
    complete = (admitted_cleanup and raw_terminal) or no_child_cleanup
    cleanup_complete = admitted_cleanup or no_child_cleanup
    failure = None if not state.failure else f"native_failure_{int(state.failure)}"
    return replace(launch.receipt(), pid=int(state.child_pid) or None,
        child_start=int(state.child_start) or None, ready_ns=int(state.ready_ns) or None,
        native_terminal_ns=int(state.native_terminal_ns) or None,
        request_bytes=int(call.input.request_bytes), request_bytes_written=int(state.request_bytes_written),
        credential_bytes_written=int(state.credential_bytes_written), credentials_transferred=bool(state.credential_bytes_written),
        stdout_bytes=int(state.stdout_bytes_observed), stdout_retained=int(state.stdout_bytes_retained),
        stderr_bytes=int(state.stderr_bytes_observed), response_bytes=int(state.body_bytes),
        stdout_eof=bool(state.stdout_eof), stderr_eof=bool(state.stderr_eof), raw_wait_status=raw,
        waited_pid=waited, raw_wait_consumed=consumed,
        returncode=os.waitstatus_to_exitcode(raw) if raw is not None else None,
        native_wait_calls=int(state.wait_calls), cancel_signal=int(state.cancel_signal) or None,
        cancel_calls=int(state.cancel_calls), constructor_returned=bool(state.spawned),
        constructor_ambiguous=False, custody_complete=complete, terminal_cleanup_complete=cleanup_complete,
        deadline_met=bool(state.deadline_met), failure=failure,
        native_failure=int(state.failure), native_stage=int(state.stage), native_errno=int(state.syscall_errno),
        spawn_rc=int(state.spawn_rc), wait_loss=bool(state.wait_loss), ready_verified=bool(state.ready_verified),
        owned_fd_count=int(state.owned_fd_count), fds_closed=bool(state.fds_closed),
        closed_fd_count=int(state.closed_fd_count), max_owned_fd_count=int(state.max_owned_fd_count),
        stdin_closed=bool(state.stdin_closed), pidfd_acquired=bool(state.pidfd_acquired),
        pidfd_terminal=bool(state.pidfd_terminal),
        protocol_complete=bool(state.protocol_complete), protocol_failure_frame=bool(state.protocol_failure_frame),
        frame_count=int(state.frame_count), data_frame_count=int(state.data_frame_count),
        read_calls=int(state.read_calls), write_calls=int(state.write_calls), poll_calls=int(state.poll_calls),
        native_phase=int(state.phase), launch_auxiliary_unobserved=bool(state.launch_auxiliary_unobserved),
        native_artifact_sha256=call.native.sha256, native_abi_sha256=call.native.abi_sha256)


def _raise_native_failure(receipt: CustodyReceipt) -> None:
    if not receipt.custody_complete or receipt.wait_loss:
        raise CustodyFailure("native_custody_failed", receipt)
    if receipt.native_failure == 14 or receipt.returncode == -int(signal.SIGALRM):
        raise DeadlineExceeded("http_native_deadline", receipt)
    if receipt.native_failure:
        raise TransportRefused("http_native_exchange_refused", receipt)


def exchange(method: str, endpoint: str, *, headers: Mapping[str, str], params: Any = None,
             data: Any = None, budget: Any, cap: int,
             receipt_sink: Callable[[CustodyReceipt], None] | None = None) -> Snapshot:
    """One already scoped physical request; no retries, token parse or writes."""
    intent = _validate_intent(method, endpoint, headers, params, data, cap)
    launch = _launch_metadata(budget)
    request = _canonical({"protocol": PROTOCOL, "nonce": launch.nonce,
                          "source_sha256": launch.source_sha256, "intent": intent})
    if len(request) > REQUEST_PAYLOAD_MAX:
        raise TransportRefused("request_frame_exhausted", launch.receipt())
    native = _native_factory()
    call = _NativeCall(native, launch, request, cap)
    receipt = launch.receipt()
    observed = False
    try:
        if time.monotonic_ns() >= launch.io_cutoff_ns:
            raise DeadlineExceeded("http_envelope_unavailable", receipt)
        call.invoke()
        # A pending Python control exception raised by the actual FFI return
        # propagates directly before this projection, body parsing or token use.
        receipt = _receipt_from_state(call)
        _raise_native_failure(receipt)
        if (not receipt.ready_verified or not receipt.protocol_complete or receipt.returncode != 0
                or receipt.request_bytes_written != receipt.request_bytes):
            raise TransportRefused("incomplete_http_exchange", receipt)
        parser = _ResponseFrames(launch, cap)
        parser.feed(call.raw_stdout())
        if (not parser.complete() or parser.ready is None or parser.ready["pid"] != receipt.pid
                or parser.ready["child_start"] != receipt.child_start
                or len(parser.content) != receipt.response_bytes
                or parser.frame_count != receipt.frame_count or parser.data_frame_count != receipt.data_frame_count):
            raise TransportRefused("incomplete_http_exchange", receipt)
        content = bytes(parser.content)
        if time.monotonic_ns() > launch.physical_end_ns:
            raise DeadlineExceeded("parent_finish_deadline", receipt)
        if receipt_sink is not None:
            observed = True
            receipt_sink(receipt)
        budget.remaining()
        # This sample includes parse, copy, callback and budget work. External
        # code observing actual public return remains the authoritative oracle.
        finished = time.monotonic_ns()
        receipt = replace(receipt, finished_ns=finished,
                          deadline_met=receipt.deadline_met and finished <= launch.original_deadline_ns)
        result = Snapshot(parser.status, parser.headers, content, receipt)
        if not receipt.deadline_met or time.monotonic_ns() > launch.physical_end_ns:
            raise DeadlineExceeded("parent_finish_deadline", receipt)
        return result
    except BaseException as error:
        # Observers run only once an actual native terminal receipt exists.
        # A second observer failure must not replace an original Python control
        # exception or allow parsing/commit after it.
        if isinstance(error, _ExchangeError):
            error.receipt = error.receipt or receipt
        if receipt_sink is not None and not observed and receipt.native_terminal_ns is not None:
            observed = True
            try:
                receipt_sink(receipt)
            except BaseException:
                pass
        raise
    finally:
        # C cannot return an incomplete owner. If Python control raised at the
        # native return, the retained POD still contains terminal facts. Keep any
        # unexpected nonterminal call alive for a hard contract failure; this
        # retention is never cleanup, a fallback reaper or an accepted result.
        if (not call.entered or (call.state.phase == 10 and call.state.owned_fd_count == 0
                                and call.state.fds_closed)):
            if native.active_call is call:
                native.active_call = None



def _parse_launch(arguments: Sequence[str]) -> _Launch:
    if len(arguments) != 11 or any(not isinstance(value, str) or len(value) > 80 for value in arguments):
        raise TransportRefused("invalid_worker_launch")
    nonce, parent, pstart, tid, tstart, original, physical, cutoff, cleanup, source, invocation = arguments
    if NONCE_PATTERN.fullmatch(nonce) is None or NONCE_PATTERN.fullmatch(source) is None:
        raise TransportRefused("invalid_worker_launch")
    numbers = [parent, pstart, tid, tstart, original, physical, cutoff, cleanup, invocation]
    if any(not re.fullmatch(r"[1-9][0-9]{0,20}", value) for value in numbers):
        raise TransportRefused("invalid_worker_launch")
    values = list(map(int, numbers))
    launch = _Launch(nonce, values[0], values[1], values[2], values[3], values[4], values[5],
                     values[6], values[7], source, values[8])
    if (launch.physical_end_ns > launch.original_deadline_ns
            or launch.physical_end_ns > launch.invocation_ns + PHYSICAL_NS
            or launch.io_cutoff_ns != launch.physical_end_ns - FINISH_RESERVE_NS
            or launch.cleanup_cutoff_ns != launch.physical_end_ns - PARENT_RESERVE_NS
            or launch.io_cutoff_ns <= launch.invocation_ns):
        raise TransportRefused("invalid_worker_deadlines")
    return launch


def _worker_guards(launch: _Launch) -> None:
    _platform_guard()
    # No pipe read or Requests import precedes these guards.
    import ctypes
    import resource
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong,
                           ctypes.c_ulong, ctypes.c_ulong]
    libc.prctl.restype = ctypes.c_int
    if libc.prctl(1, int(signal.SIGKILL), 0, 0, 0) != 0:  # PR_SET_PDEATHSIG
        raise TransportRefused("parent_death_guard_unavailable")
    if os.getppid() != launch.parent_pid or _identity(launch.parent_pid)[3] != launch.parent_start:
        raise TransportRefused("parent_identity_changed")
    if _identity(launch.parent_pid, tid=launch.creator_tid)[3] != launch.creator_start:
        raise TransportRefused("creator_thread_identity_changed")
    signal.signal(signal.SIGALRM, signal.SIG_DFL)
    signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGALRM})
    remaining_ns = launch.io_cutoff_ns - time.monotonic_ns()
    if remaining_ns <= 0:
        raise DeadlineExceeded("worker_deadline_expired")
    signal.setitimer(signal.ITIMER_REAL, remaining_ns / 1_000_000_000, 0.0)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_AS, (ADDRESS_SPACE_MAX, ADDRESS_SPACE_MAX))
    resource.setrlimit(resource.RLIMIT_NOFILE, (DESCRIPTORS_MAX, DESCRIPTORS_MAX))
    if (os.getppid() != launch.parent_pid or _identity(launch.parent_pid)[3] != launch.parent_start
            or _identity(launch.parent_pid, tid=launch.creator_tid)[3] != launch.creator_start):
        raise TransportRefused("creator_identity_changed_after_guard")
    if time.monotonic_ns() >= launch.io_cutoff_ns:
        raise DeadlineExceeded("worker_deadline_expired")
    if _source_hash() != launch.source_sha256:
        raise TransportRefused("worker_source_identity_mismatch")
    if len(os.listdir("/proc/self/task")) != 1:
        raise TransportRefused("worker_thread_refused")


def _read_exact(descriptor: int, count: int) -> bytes:
    result = bytearray()
    while len(result) < count:
        block = os.read(descriptor, count - len(result))
        if not block:
            raise TransportRefused("truncated_private_request")
        result.extend(block)
    return bytes(result)


def _write_all(descriptor: int, value: bytes) -> None:
    offset = 0
    while offset < len(value):
        sent = os.write(descriptor, value[offset:])
        if sent <= 0:
            raise TransportRefused("private_response_pipe_closed")
        offset += sent


def _requests_session():
    # Native routing fixtures may replace the real connection route at this
    # private factory; production has no caller-selected URL or test flag.
    import requests
    import urllib3
    if requests.__version__ != "2.34.2" or urllib3.__version__ != "2.7.0":
        raise TransportRefused("worker_dependency_identity_mismatch")
    session = requests.Session()
    session.trust_env = False
    adapter = requests.adapters.HTTPAdapter(pool_connections=1, pool_maxsize=1,
                                            max_retries=0, pool_block=True)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def _worker_request(launch: _Launch) -> dict[str, Any]:
    length, = struct.unpack("!I", _read_exact(0, 4))
    if not 0 < length <= REQUEST_PAYLOAD_MAX:
        raise TransportRefused("invalid_request_frame")
    document = _decode_json(_read_exact(0, length), REQUEST_PAYLOAD_MAX)
    if os.read(0, 1):
        raise TransportRefused("invalid_request_frame")
    if (not isinstance(document, dict) or set(document) != {"protocol", "nonce", "source_sha256", "intent"}
            or document["protocol"] != PROTOCOL or document["nonce"] != launch.nonce
            or document["source_sha256"] != launch.source_sha256 or not isinstance(document["intent"], dict)):
        raise TransportRefused("invalid_request_frame")
    intent = document["intent"]
    if set(intent) != {"method", "endpoint", "headers", "params", "data", "cap"}:
        raise TransportRefused("invalid_request_frame")
    return _validate_intent(intent["method"], intent["endpoint"], intent["headers"],
                            intent["params"], intent["data"], intent["cap"])


def _worker_exchange(launch: _Launch, intent: dict[str, Any]) -> None:
    session, response = None, None
    try:
        session = _requests_session()
        remaining = (launch.io_cutoff_ns - time.monotonic_ns()) / 1_000_000_000
        if remaining <= 0:
            raise DeadlineExceeded("worker_deadline_expired")
        response = session.request(intent["method"], intent["endpoint"], headers=intent["headers"],
                                   params=intent["params"], data=intent["data"],
                                   timeout=remaining, verify=True, allow_redirects=False,
                                   stream=True, proxies={})
        encoding = response.headers.get("Content-Encoding", "").strip().lower()
        if encoding not in {"", "identity"}:
            raise TransportRefused("response_encoding_refused")
        headers = {"nonce": launch.nonce, "status": response.status_code,
                   "headers": [[str(key), str(value)] for key, value in response.headers.items()]}
        packed = _canonical(headers)
        if len(packed) > CONTROL_PAYLOAD_MAX:
            raise TransportRefused("response_headers_exhausted")
        _write_all(1, _frame(b"H", packed))
        count, digest, pending = 0, hashlib.sha256(), bytearray()
        for chunk in response.iter_content(chunk_size=FRAME_DATA_MAX):
            if time.monotonic_ns() >= launch.io_cutoff_ns:
                raise DeadlineExceeded("worker_deadline_expired")
            if not chunk:
                continue
            count += len(chunk)
            if count > intent["cap"]:
                raise TransportRefused("response_bytes_exhausted")
            digest.update(chunk)
            pending.extend(chunk)
            # urllib3 may yield smaller reads. Coalesce them; only the final D
            # may be short, so frame counts remain a strict function of cap.
            while len(pending) >= FRAME_DATA_MAX:
                _write_all(1, _frame(b"D", bytes(pending[:FRAME_DATA_MAX])))
                del pending[:FRAME_DATA_MAX]
        if pending:
            _write_all(1, _frame(b"D", bytes(pending)))
        response.close()
        response = None
        session.close()
        session = None
        if len(os.listdir("/proc/self/task")) != 1:
            raise TransportRefused("worker_resource_refused")
        if time.monotonic_ns() >= launch.io_cutoff_ns:
            raise DeadlineExceeded("worker_deadline_expired")
        _write_all(1, _frame(b"E", _canonical({"nonce": launch.nonce, "bytes": count,
                                               "sha256": digest.hexdigest()})))
    finally:
        if response is not None:
            response.close()
        if session is not None:
            session.close()


def _worker_main(arguments: Sequence[str]) -> int:
    """Standalone worker entry.  Its stdout is exclusively bounded protocol."""
    launch, ready = None, False
    try:
        launch = _parse_launch(arguments)
        _worker_guards(launch)
        child = _identity(os.getpid())
        _write_all(1, MAGIC + _frame(b"R", _ready_payload(launch, child)))
        ready = True
        intent = _worker_request(launch)
        _worker_exchange(launch, intent)
        return 0
    except BaseException as error:
        # Do not print the exception or traceback: Requests exceptions can embed
        # prepared Authorization headers, token forms or provider response bodies.
        if ready and launch is not None:
            allowed = {"response_bytes_exhausted", "response_encoding_refused",
                       "response_headers_exhausted", "invalid_request_frame", "worker_resource_refused",
                       "worker_deadline_expired"}
            code = error.code if isinstance(error, _ExchangeError) and error.code in allowed else "transport_failed"
            try:
                _write_all(1, _frame(b"F", _canonical({"code": code, "nonce": launch.nonce})))
            except BaseException:
                pass
        else:
            try:
                _write_all(2, b"qbo_http_worker_refused\n")
            except BaseException:
                pass
        return 70


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "--worker":
        os._exit(_worker_main(sys.argv[2:]))
    os._exit(64)

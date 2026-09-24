from __future__ import annotations

import ctypes
import os
import sys
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence


@dataclass(frozen=True, slots=True)
class ProcessLivenessDiagnostic:
    """Structured, non-mutating process liveness/ownership evidence."""

    pid: int | None
    alive: bool
    identity_verified: bool
    reason: str
    expected_command_parts: tuple[str, ...] = ()
    observed_command: str | None = None
    expected_started_at: float | None = None
    observed_creation_time: float | None = None
    native_error: str | None = None
    identity_detail: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "alive": self.alive,
            "identity_verified": self.identity_verified,
            "reason": self.reason,
            "expected_command_parts": list(self.expected_command_parts),
            "observed_command": self.observed_command,
            "expected_started_at": self.expected_started_at,
            "observed_creation_time": self.observed_creation_time,
            "native_error": self.native_error,
            "identity_detail": self.identity_detail,
        }


@dataclass(frozen=True, slots=True)
class _DarwinNativeIdentity:
    creation: float | None
    command: str | None
    native_error: str | None = None
    command_unavailable_reason: str | None = None


def _timestamp(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def _windows_process(pid: int) -> tuple[bool, float | None]:
    """Query Windows process state without invoking TerminateProcess."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.GetProcessTimes.argtypes = (wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False, None
    try:
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return False, None
        created = ctypes.c_ulonglong()
        exited = ctypes.c_ulonglong()
        kernel = ctypes.c_ulonglong()
        user = ctypes.c_ulonglong()
        creation = None
        if kernel32.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel), ctypes.byref(user)):
            creation = (created.value - 116444736000000000) / 10_000_000
        return exit_code.value == 259, creation  # STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


class _DarwinProcBsdInfo(ctypes.Structure):
    _fields_ = [
        ("pbi_flags", ctypes.c_uint32), ("pbi_status", ctypes.c_uint32),
        ("pbi_xstatus", ctypes.c_uint32), ("pbi_pid", ctypes.c_uint32),
        ("pbi_ppid", ctypes.c_uint32), ("pbi_uid", ctypes.c_uint32),
        ("pbi_gid", ctypes.c_uint32), ("pbi_ruid", ctypes.c_uint32),
        ("pbi_rgid", ctypes.c_uint32), ("pbi_svuid", ctypes.c_uint32),
        ("pbi_svgid", ctypes.c_uint32), ("rfu_1", ctypes.c_uint32),
        ("pbi_comm", ctypes.c_char * 16), ("pbi_name", ctypes.c_char * 32),
        ("pbi_nfiles", ctypes.c_uint32), ("pbi_pgid", ctypes.c_uint32),
        ("pbi_pjobc", ctypes.c_uint32), ("e_tdev", ctypes.c_uint32),
        ("e_tpgid", ctypes.c_uint32), ("pbi_nice", ctypes.c_int32),
        ("pbi_start_tvsec", ctypes.c_uint64), ("pbi_start_tvusec", ctypes.c_uint64),
    ]


def _looks_like_environment_record(value: str) -> bool:
    """Reject env/apple-vector records when argv has been erased or mutated."""
    key, sep, _ = value.partition("=")
    if not sep or not key:
        return False
    if not (key[0].isalpha() or key[0] == "_"):
        return False
    return all(char.isalnum() or char == "_" for char in key)


def _parse_darwin_procargs(raw: bytes) -> tuple[str | None, str | None]:
    """Parse only argv from a KERN_PROCARGS2 payload without leaking env/apple data."""
    if len(raw) <= 4:
        return None, "PROCARGS_BUFFER_TOO_SMALL"
    argc = int.from_bytes(raw[:4], byteorder=sys.byteorder, signed=True)
    if argc <= 0:
        return None, "PROCARGS_ARGC_INVALID"

    exec_end = raw.find(b"\0", 4)
    if exec_end < 0:
        return None, "PROCARGS_EXEC_PATH_UNTERMINATED"
    offset = exec_end + 1

    # XNU may leave NUL padding between the saved exec_path and argv[0].
    while offset < len(raw) and raw[offset] == 0:
        offset += 1
    if offset >= len(raw):
        return None, "PROCARGS_ARGV_UNAVAILABLE"

    argv: list[str] = []
    for index in range(argc):
        if offset >= len(raw):
            return None, "PROCARGS_ARGV_TRUNCATED"
        end = raw.find(b"\0", offset)
        if end < 0:
            return None, "PROCARGS_ARGV_UNTERMINATED"
        value = raw[offset:end].decode("utf-8", "replace")

        # KERN_PROCARGS2 reflects mutable process stack storage. Long-running
        # runtimes can erase/rewrite argv; skipping the resulting NUL area can
        # land on envp/apple[] (observed as ptr_munge= on real macOS PAPER
        # workers). Never classify that auxiliary data as an observed command.
        if index == 0 and _looks_like_environment_record(value):
            return None, "PROCARGS_ARGV_MUTATED_TO_AUXILIARY_VECTOR"

        argv.append(value)
        offset = end + 1

    return " ".join(argv), None


def _darwin_native_identity_details(pid: int) -> _DarwinNativeIdentity:
    """Read Darwin process start time and argv while retaining probe diagnostics."""
    native_errors: list[str] = []
    creation: float | None = None

    try:
        libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        libproc.proc_pidinfo.argtypes = (
            ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int,
        )
        libproc.proc_pidinfo.restype = ctypes.c_int
        info = _DarwinProcBsdInfo()
        size = ctypes.sizeof(info)
        read = libproc.proc_pidinfo(pid, 3, 0, ctypes.byref(info), size)  # PROC_PIDTBSDINFO
        if read == size:
            creation = float(info.pbi_start_tvsec) + float(info.pbi_start_tvusec) / 1_000_000.0
        else:
            native_errors.append(f"proc_pidinfo:read={read}:errno={ctypes.get_errno()}")

        libsystem = ctypes.CDLL(None, use_errno=True)
        libsystem.sysctl.argtypes = (
            ctypes.POINTER(ctypes.c_int), ctypes.c_uint, ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p, ctypes.c_size_t,
        )
        libsystem.sysctl.restype = ctypes.c_int

        # Follow Darwin's established KERN_PROCARGS2 consumers: allocate the
        # KERN_ARGMAX buffer rather than trusting a size-only PROCARGS2 query.
        argmax_mib = (ctypes.c_int * 2)(1, 8)  # CTL_KERN, KERN_ARGMAX
        argmax = ctypes.c_int(0)
        argmax_size = ctypes.c_size_t(ctypes.sizeof(argmax))
        if libsystem.sysctl(argmax_mib, 2, ctypes.byref(argmax), ctypes.byref(argmax_size), None, 0) != 0 or argmax.value <= 4:
            native_errors.append(f"kern_argmax:errno={ctypes.get_errno()}")
            return _DarwinNativeIdentity(
                creation=creation,
                command=None,
                native_error=";".join(native_errors) or None,
                command_unavailable_reason="KERN_ARGMAX_UNAVAILABLE",
            )

        mib = (ctypes.c_int * 3)(1, 49, pid)  # CTL_KERN, KERN_PROCARGS2
        argv_size = ctypes.c_size_t(argmax.value)
        argv_buffer = ctypes.create_string_buffer(argmax.value)
        if libsystem.sysctl(mib, 3, argv_buffer, ctypes.byref(argv_size), None, 0) != 0:
            native_errors.append(f"kern_procargs2:errno={ctypes.get_errno()}")
            return _DarwinNativeIdentity(
                creation=creation,
                command=None,
                native_error=";".join(native_errors) or None,
                command_unavailable_reason="KERN_PROCARGS2_UNAVAILABLE",
            )

        raw = argv_buffer.raw[:argv_size.value]
        command, command_reason = _parse_darwin_procargs(raw)
        return _DarwinNativeIdentity(
            creation=creation,
            command=command,
            native_error=";".join(native_errors) or None,
            command_unavailable_reason=command_reason,
        )
    except (AttributeError, OSError, ValueError) as exc:
        native_errors.append(f"{exc.__class__.__name__}:{exc}")
        return _DarwinNativeIdentity(
            creation=creation,
            command=None,
            native_error=";".join(native_errors),
            command_unavailable_reason="DARWIN_IDENTITY_EXCEPTION",
        )


def _darwin_native_identity(pid: int) -> tuple[float | None, str | None]:
    """Backward-compatible Darwin identity tuple."""
    details = _darwin_native_identity_details(pid)
    return details.creation, details.command


_DARWIN_NATIVE_IDENTITY_IMPL = _darwin_native_identity


def _darwin_process_details(pid: int) -> tuple[bool, float | None, str | None, str | None, str | None]:
    """Query macOS process identity without relying on Linux /proc."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False, None, None, None, None
    except PermissionError:
        pass
    except OSError as exc:
        return False, None, None, f"os.kill:{exc.__class__.__name__}:{exc}", None

    if _darwin_native_identity is not _DARWIN_NATIVE_IDENTITY_IMPL:
        creation, command = _darwin_native_identity(pid)
        return True, creation, command, None, None

    details = _darwin_native_identity_details(pid)
    return (
        True,
        details.creation,
        details.command,
        details.native_error,
        details.command_unavailable_reason,
    )


def _darwin_process(pid: int) -> tuple[bool, float | None, str | None]:
    alive, creation, command, _native_error, _identity_detail = _darwin_process_details(pid)
    return alive, creation, command


_DARWIN_PROCESS_IMPL = _darwin_process


def _posix_process(pid: int) -> tuple[bool, float | None, str | None]:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False, None, None
    except PermissionError:
        pass
    except OSError:
        return False, None, None
    proc = Path(f"/proc/{pid}")
    cmdline = None
    creation = None
    try:
        cmdline = (proc / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
    except OSError:
        pass
    try:
        fields = (proc / "stat").read_text().split()
        ticks = os.sysconf("SC_CLK_TCK")
        boot = float(next(line.split()[1] for line in Path("/proc/stat").read_text().splitlines() if line.startswith("btime ")))
        creation = boot + float(fields[21]) / float(ticks)
    except (OSError, ValueError, IndexError, StopIteration):
        pass
    return True, creation, cmdline


def process_liveness_diagnostics(
    pid: Any,
    *,
    expected_command_parts: Sequence[str] = (),
    expected_started_at: Any = None,
    creation_tolerance_seconds: float = 120.0,
) -> ProcessLivenessDiagnostic:
    """Return structured liveness evidence without mutating or signaling the target."""
    required = tuple(str(part).strip().lower() for part in expected_command_parts if str(part).strip())
    expected_time = _timestamp(expected_started_at)
    try:
        number = int(pid)
    except (TypeError, ValueError):
        return ProcessLivenessDiagnostic(
            pid=None,
            alive=False,
            identity_verified=False,
            reason="PID_ABSENT",
            expected_command_parts=required,
            expected_started_at=expected_time,
            identity_detail="INVALID_PID",
        )
    if number <= 0:
        return ProcessLivenessDiagnostic(
            pid=number,
            alive=False,
            identity_verified=False,
            reason="PID_ABSENT",
            expected_command_parts=required,
            expected_started_at=expected_time,
            identity_detail="INVALID_PID",
        )

    native_error: str | None = None
    identity_detail: str | None = None
    if os.name == "nt":
        alive, creation = _windows_process(number)
        cmdline = None
    elif sys.platform == "darwin":
        if _darwin_process is not _DARWIN_PROCESS_IMPL:
            alive, creation, cmdline = _darwin_process(number)
        else:
            alive, creation, cmdline, native_error, identity_detail = _darwin_process_details(number)
    else:
        alive, creation, cmdline = _posix_process(number)

    if not alive:
        reason = "DARWIN_NATIVE_PROBE_ERROR" if sys.platform == "darwin" and native_error else "PID_ABSENT"
        return ProcessLivenessDiagnostic(
            pid=number,
            alive=False,
            identity_verified=False,
            reason=reason,
            expected_command_parts=required,
            observed_command=cmdline,
            expected_started_at=expected_time,
            observed_creation_time=creation,
            native_error=native_error,
            identity_detail=identity_detail,
        )

    if expected_time is not None and creation is not None:
        if abs(creation - expected_time) > float(creation_tolerance_seconds):
            return ProcessLivenessDiagnostic(
                pid=number,
                alive=False,
                identity_verified=False,
                reason="PROCESS_CREATION_TIME_MISMATCH",
                expected_command_parts=required,
                observed_command=cmdline,
                expected_started_at=expected_time,
                observed_creation_time=creation,
                native_error=native_error,
                identity_detail=identity_detail,
            )

    if required and cmdline is not None:
        observed = cmdline.lower()
        if not all(part in observed for part in required):
            return ProcessLivenessDiagnostic(
                pid=number,
                alive=False,
                identity_verified=False,
                reason="EXPECTED_COMMAND_MISMATCH",
                expected_command_parts=required,
                observed_command=cmdline,
                expected_started_at=expected_time,
                observed_creation_time=creation,
                native_error=native_error,
                identity_detail=identity_detail,
            )

    # Windows cannot retrieve the command line through the query-only handle.
    # Creation time is therefore mandatory when ownership is requested.
    if required and os.name == "nt" and expected_time is None:
        return ProcessLivenessDiagnostic(
            pid=number,
            alive=False,
            identity_verified=False,
            reason="COMMAND_IDENTITY_UNAVAILABLE",
            expected_command_parts=required,
            observed_command=None,
            expected_started_at=expected_time,
            observed_creation_time=creation,
            identity_detail="WINDOWS_QUERY_ONLY_COMMAND_UNAVAILABLE",
        )

    if sys.platform == "darwin" and native_error:
        return ProcessLivenessDiagnostic(
            pid=number,
            alive=True,
            identity_verified=False,
            reason="DARWIN_NATIVE_PROBE_ERROR",
            expected_command_parts=required,
            observed_command=cmdline,
            expected_started_at=expected_time,
            observed_creation_time=creation,
            native_error=native_error,
            identity_detail=identity_detail,
        )

    if required and cmdline is None and os.name != "nt":
        return ProcessLivenessDiagnostic(
            pid=number,
            alive=True,
            identity_verified=False,
            reason="COMMAND_IDENTITY_UNAVAILABLE",
            expected_command_parts=required,
            observed_command=None,
            expected_started_at=expected_time,
            observed_creation_time=creation,
            native_error=native_error,
            identity_detail=identity_detail,
        )

    if expected_time is not None and creation is None:
        return ProcessLivenessDiagnostic(
            pid=number,
            alive=True,
            identity_verified=False,
            reason="PROCESS_CREATION_TIME_UNAVAILABLE",
            expected_command_parts=required,
            observed_command=cmdline,
            expected_started_at=expected_time,
            observed_creation_time=None,
            native_error=native_error,
            identity_detail=identity_detail,
        )

    return ProcessLivenessDiagnostic(
        pid=number,
        alive=True,
        identity_verified=True,
        reason="VERIFIED",
        expected_command_parts=required,
        observed_command=cmdline,
        expected_started_at=expected_time,
        observed_creation_time=creation,
        native_error=native_error,
        identity_detail=identity_detail,
    )


def process_is_alive(
    pid: Any,
    *,
    expected_command_parts: Sequence[str] = (),
    expected_started_at: Any = None,
    creation_tolerance_seconds: float = 120.0,
) -> bool:
    """Backward-compatible boolean wrapper around structured liveness evidence."""
    return process_liveness_diagnostics(
        pid,
        expected_command_parts=expected_command_parts,
        expected_started_at=expected_started_at,
        creation_tolerance_seconds=creation_tolerance_seconds,
    ).alive

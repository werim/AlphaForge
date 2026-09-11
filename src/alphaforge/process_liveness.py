from __future__ import annotations

import ctypes
import os
import sys
from ctypes import wintypes
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence


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


def _darwin_native_identity(pid: int) -> tuple[float | None, str | None]:
    """Read process start time and argv through native query-only Darwin APIs."""
    try:
        libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        libproc.proc_pidinfo.argtypes = (
            ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int,
        )
        libproc.proc_pidinfo.restype = ctypes.c_int
        info = _DarwinProcBsdInfo()
        size = ctypes.sizeof(info)
        read = libproc.proc_pidinfo(pid, 3, 0, ctypes.byref(info), size)  # PROC_PIDTBSDINFO
        creation = None if read != size else float(info.pbi_start_tvsec) + float(info.pbi_start_tvusec) / 1_000_000.0

        libsystem = ctypes.CDLL(None, use_errno=True)
        libsystem.sysctl.argtypes = (
            ctypes.POINTER(ctypes.c_int), ctypes.c_uint, ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p, ctypes.c_size_t,
        )
        libsystem.sysctl.restype = ctypes.c_int
        mib = (ctypes.c_int * 3)(1, 49, pid)  # CTL_KERN, KERN_PROCARGS2
        argv_size = ctypes.c_size_t(0)
        if libsystem.sysctl(mib, 3, None, ctypes.byref(argv_size), None, 0) != 0 or argv_size.value <= 4:
            return creation, None
        argv_buffer = ctypes.create_string_buffer(argv_size.value)
        if libsystem.sysctl(mib, 3, argv_buffer, ctypes.byref(argv_size), None, 0) != 0:
            return creation, None
        raw = argv_buffer.raw[:argv_size.value]
        argc = int.from_bytes(raw[:4], byteorder=sys.byteorder, signed=True)
        if argc <= 0:
            return creation, None
        offset = raw.find(b"\0", 4)
        if offset < 0:
            return creation, None
        offset += 1
        while offset < len(raw) and raw[offset] == 0:
            offset += 1
        argv: list[str] = []
        while offset < len(raw) and len(argv) < argc:
            end = raw.find(b"\0", offset)
            if end < 0:
                break
            argv.append(raw[offset:end].decode("utf-8", "replace"))
            offset = end + 1
        return creation, " ".join(argv) if len(argv) == argc else None
    except (AttributeError, OSError, ValueError):
        return None, None


def _darwin_process(pid: int) -> tuple[bool, float | None, str | None]:
    """Query macOS process identity without relying on Linux /proc."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False, None, None
    except PermissionError:
        pass
    except OSError:
        return False, None, None

    creation, command = _darwin_native_identity(pid)
    return True, creation, command


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


def process_is_alive(
    pid: Any,
    *,
    expected_command_parts: Sequence[str] = (),
    expected_started_at: Any = None,
    creation_tolerance_seconds: float = 120.0,
) -> bool:
    """Non-mutating process liveness and optional recycled-PID identity probe."""
    try:
        number = int(pid)
    except (TypeError, ValueError):
        return False
    if number <= 0:
        return False
    if os.name == "nt":
        alive, creation = _windows_process(number)
        cmdline = None
    elif sys.platform == "darwin":
        alive, creation, cmdline = _darwin_process(number)
    else:
        alive, creation, cmdline = _posix_process(number)
    if not alive:
        return False
    expected_time = _timestamp(expected_started_at)
    if expected_time is not None and creation is not None:
        if abs(creation - expected_time) > float(creation_tolerance_seconds):
            return False
    required = [str(part).strip().lower() for part in expected_command_parts if str(part).strip()]
    if required and cmdline is not None:
        observed = cmdline.lower()
        if not all(part in observed for part in required):
            return False
    # Windows cannot retrieve a command line through the query-only handle;
    # creation time is therefore mandatory when ownership is requested. If a
    # platform probe cannot read identity, do not report an existing process as
    # dead: callers can conservatively block duplicate ownership.
    if required and os.name == "nt" and expected_time is None:
        return False
    return True

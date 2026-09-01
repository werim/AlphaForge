from __future__ import annotations

import ctypes
import os
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
    else:
        alive, creation, cmdline = _posix_process(number)
    if not alive:
        return False
    expected_time = _timestamp(expected_started_at)
    if expected_time is not None:
        if creation is None or abs(creation - expected_time) > float(creation_tolerance_seconds):
            return False
    required = [str(part).strip().lower() for part in expected_command_parts if str(part).strip()]
    if required and cmdline is not None:
        observed = cmdline.lower()
        if not all(part in observed for part in required):
            return False
    # Windows cannot retrieve a command line through the query-only handle;
    # creation time is therefore mandatory when ownership is requested.
    if required and os.name == "nt" and expected_time is None:
        return False
    return True

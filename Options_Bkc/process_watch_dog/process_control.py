from __future__ import annotations

import ctypes
import errno
import os
import signal
import subprocess
import time
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path


def popen_platform_options() -> dict[str, object]:
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


@dataclass
class ProcessJob:
    """Best-effort Windows job object that kills descendants when closed."""

    handle: int | None = None

    @classmethod
    def attach(cls, process: subprocess.Popen[str]) -> "ProcessJob":
        if os.name != "nt":
            return cls()
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            return cls()
        job = cls(int(handle))
        try:
            _configure_kill_on_close(kernel32, handle)
            if not kernel32.AssignProcessToJobObject(
                wintypes.HANDLE(handle), wintypes.HANDLE(process._handle)  # type: ignore[attr-defined]
            ):
                job.close()
                return cls()
        except Exception:
            job.close()
            return cls()
        return job

    def terminate(self, exit_code: int = 1) -> bool:
        if os.name != "nt" or self.handle is None:
            return False
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.TerminateJobObject.restype = wintypes.BOOL
        return bool(
            kernel32.TerminateJobObject(
                wintypes.HANDLE(self.handle), wintypes.UINT(exit_code)
            )
        )

    def close(self) -> None:
        if os.name == "nt" and self.handle is not None:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            kernel32.CloseHandle(wintypes.HANDLE(self.handle))
            self.handle = None


def terminate_process_tree(
    process: subprocess.Popen[str],
    *,
    job: ProcessJob | None,
    graceful_timeout_seconds: float,
) -> int | None:
    """Request a graceful stop, then force the entire managed process tree."""

    if process.poll() is not None:
        return process.returncode
    _send_graceful_signal(process)
    deadline = time.monotonic() + graceful_timeout_seconds
    while process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    if process.poll() is not None:
        return process.returncode

    if job is not None and job.terminate():
        try:
            return process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass

    if os.name == "nt":
        subprocess.run(
            ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        return process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            return process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            return process.poll()


def process_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        process_query_limited_information = 0x1000
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(
            process_query_limited_information,
            False,
            wintypes.DWORD(pid),
        )
        if handle:
            kernel32.CloseHandle(handle)
            return True
        return ctypes.get_last_error() == 5
    try:
        os.kill(pid, 0)
    except OSError as exc:
        return exc.errno == errno.EPERM
    return True


class InstanceLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._owned = False

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _attempt in range(2):
            try:
                descriptor = os.open(
                    self.path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o600,
                )
            except FileExistsError:
                stale_pid = self._read_pid()
                if stale_pid is not None and process_exists(stale_pid):
                    raise RuntimeError(
                        f"another watchdog is already running with PID {stale_pid}"
                    )
                try:
                    self.path.unlink()
                except FileNotFoundError:
                    pass
                continue
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(str(os.getpid()))
                handle.flush()
                os.fsync(handle.fileno())
            self._owned = True
            return
        raise RuntimeError(f"could not acquire watchdog lock: {self.path}")

    def release(self) -> None:
        if not self._owned:
            return
        try:
            if self._read_pid() == os.getpid():
                self.path.unlink(missing_ok=True)
        finally:
            self._owned = False

    def _read_pid(self) -> int | None:
        try:
            return int(self.path.read_text(encoding="utf-8").strip())
        except (FileNotFoundError, OSError, ValueError):
            return None


def _send_graceful_signal(process: subprocess.Popen[str]) -> None:
    try:
        if os.name == "nt":
            process.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(process.pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        pass


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def _configure_kill_on_close(kernel32: object, handle: int) -> None:
    job_object_extended_limit_information = 9
    job_object_limit_kill_on_job_close = 0x00002000
    info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = job_object_limit_kill_on_job_close
    kernel32.SetInformationJobObject.argtypes = [  # type: ignore[attr-defined]
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL  # type: ignore[attr-defined]
    success = kernel32.SetInformationJobObject(  # type: ignore[attr-defined]
        wintypes.HANDLE(handle),
        job_object_extended_limit_information,
        ctypes.byref(info),
        ctypes.sizeof(info),
    )
    if not success:
        raise ctypes.WinError(ctypes.get_last_error())

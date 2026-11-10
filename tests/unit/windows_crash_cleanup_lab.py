"""Fixed synthetic delete-on-close/Job Object crash probe; not production recovery.

No repository source or paths are accepted. The supervisor creates a disposable
workspace fixture, then deliberately stops its own test parent. This demonstrates
a Windows primitive, not secure erasure or a complete hostile-worker boundary.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import subprocess
import sys
import tempfile
import time
from ctypes import wintypes
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parents[3]
from windows_appcontainer_lab import _ExtendedLimits


def kernel_api():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                  wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]; kernel.CloseHandle.restype = wintypes.BOOL
    kernel.ReadFile.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
    kernel.ReadFile.restype = wintypes.BOOL
    kernel.WriteFile.argtypes = [wintypes.HANDLE, wintypes.LPCVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
    kernel.WriteFile.restype = wintypes.BOOL
    kernel.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]; kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    kernel.SetInformationJobObject.restype = wintypes.BOOL
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]; kernel.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]; kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]; kernel.WaitForSingleObject.restype = wintypes.DWORD
    return kernel


def await_path(path, process=None, timeout=10):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if path.exists(): return
        if process is not None and process.poll() is not None:
            raise RuntimeError("Synthetic process exited before its ready signal")
        time.sleep(.03)
    raise RuntimeError("Synthetic process did not become ready")


def open_fixture(kernel, path, access, disposition, flags=0):
    handle = kernel.CreateFileW(str(path), access, 7, None, disposition, flags, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    return handle


def worker(root):
    kernel = kernel_api()
    await_path(root / "go")
    handle = open_fixture(kernel, root / "synthetic-input.bin", 0x80000000, 3)
    try:
        buffer = ctypes.create_string_buffer(32); count = wintypes.DWORD()
        if not kernel.ReadFile(handle, buffer, len(buffer), ctypes.byref(count), None) or buffer.raw[:count.value] != b"synthetic-only":
            raise RuntimeError("Synthetic worker did not read the fixture")
        (root / "worker-ready").write_text("ready", encoding="ascii")
        time.sleep(30)
    finally:
        kernel.CloseHandle(handle)


def parent(root):
    kernel = kernel_api(); job = None; handle = None; child = None
    try:
        handle = open_fixture(kernel, root / "synthetic-input.bin", 0x80000000 | 0x40000000 | 0x00010000, 1, 0x04000000 | 0x100)
        payload = ctypes.create_string_buffer(b"synthetic-only"); count = wintypes.DWORD()
        if not kernel.WriteFile(handle, payload, 14, ctypes.byref(count), None) or count.value != 14:
            raise RuntimeError("Could not write synthetic fixture")
        job = kernel.CreateJobObjectW(None, None)
        if not job: raise ctypes.WinError(ctypes.get_last_error())
        limits = _ExtendedLimits(); limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            raise ctypes.WinError(ctypes.get_last_error())
        child = subprocess.Popen([sys.executable, __file__, "--worker", str(root)], close_fds=True,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if not kernel.AssignProcessToJobObject(job, int(child._handle)): raise ctypes.WinError(ctypes.get_last_error())
        (root / "go").write_text("go", encoding="ascii")
        await_path(root / "worker-ready", child)
        (root / "parent-ready").write_text(str(child.pid), encoding="ascii")
        time.sleep(30)
    finally:
        if job: kernel.CloseHandle(job)
        if child and child.poll() is None:
            child.kill(); child.wait(timeout=10)
        if handle: kernel.CloseHandle(handle)


def supervisor():
    if os.name != "nt": raise RuntimeError("Windows required")
    with tempfile.TemporaryDirectory(prefix="elucidary-crash-probe-", dir=WORKSPACE) as temporary:
        root = Path(temporary).resolve()
        if root.parent != WORKSPACE: raise RuntimeError("Synthetic fixture escaped workspace")
        sentinel = root / "unrelated-sentinel"; sentinel.write_bytes(b"untouched")
        process = subprocess.Popen([sys.executable, __file__, "--parent", str(root)], close_fds=True,
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        child_handle = None; kernel = kernel_api()
        try:
            await_path(root / "parent-ready", process)
            pid = int((root / "parent-ready").read_text(encoding="ascii"))
            child_handle = kernel.OpenProcess(0x00100000, False, pid)
            if not child_handle: raise ctypes.WinError(ctypes.get_last_error())
            process.kill(); process.wait(timeout=10)
            if kernel.WaitForSingleObject(child_handle, 10000) != 0: raise RuntimeError("Synthetic child did not terminate")
            end = time.monotonic() + 5
            while (root / "synthetic-input.bin").exists() and time.monotonic() < end: time.sleep(.03)
            if (root / "synthetic-input.bin").exists(): raise RuntimeError("Delete-on-close fixture remained after both processes exited")
            if sentinel.read_bytes() != b"untouched": raise RuntimeError("Unrelated synthetic sentinel changed")
            return {"parent_abruptly_stopped": True, "child_exit_confirmed": True,
                    "delete_on_close_file_removed": True, "unrelated_sentinel_unchanged": True,
                    "secure_erasure_tested": False, "appcontainer_tested": False}
        finally:
            if process.poll() is None: process.kill(); process.wait(timeout=10)
            if child_handle: kernel.CloseHandle(child_handle)
            if process.stderr: process.stderr.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-synthetic-crash", action="store_true")
    parser.add_argument("--parent", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--worker", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.parent or args.worker:
        root = (args.parent or args.worker).resolve()
        if root.parent != WORKSPACE or not root.name.startswith("elucidary-crash-probe-"):
            raise RuntimeError("Invalid synthetic fixture")
        return parent(root) if args.parent else worker(root)
    if not args.allow_synthetic_crash: parser.error("explicit --allow-synthetic-crash is required")
    print(json.dumps(supervisor(), sort_keys=True))


if __name__ == "__main__": main()

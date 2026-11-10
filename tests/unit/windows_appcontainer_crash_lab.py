"""Synthetic AppContainer crash probe; not a production cleanup implementation.

The supervisor owns a freshly created profile and deletes only that profile and
the named disposable fixture. The fixed mock receives synthetic text only.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from ctypes import wintypes
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parents[3]


def disposable_fixture(report):
    """Resolve only a synthetic fixture created directly beneath this workspace."""
    candidate = Path(report["fixture"])
    if not candidate.is_absolute() or candidate.parent != WORKSPACE or not candidate.name.startswith("elucidary-appcontainer-probe-"):
        raise RuntimeError("Invalid synthetic fixture path")
    if candidate.exists():
        if candidate.resolve(strict=True) != candidate:
            raise RuntimeError("Synthetic fixture path changed")
        for root, directories, files in os.walk(candidate, followlinks=False):
            for name in directories + files:
                entry = Path(root) / name
                if entry.is_symlink() or getattr(entry.lstat(), "st_file_attributes", 0) & 0x400:
                    raise RuntimeError("Synthetic fixture contains a reparse point")
    return candidate


def run():
    if os.name != "nt": raise RuntimeError("Windows required")
    userenv = ctypes.WinDLL("userenv", use_last_error=True)
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    userenv.CreateAppContainerProfile.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.LPVOID)]
    userenv.CreateAppContainerProfile.restype = ctypes.c_long
    userenv.DeleteAppContainerProfile.argtypes = [wintypes.LPCWSTR]; userenv.DeleteAppContainerProfile.restype = ctypes.c_long
    advapi.FreeSid.argtypes = [wintypes.LPVOID]; advapi.FreeSid.restype = wintypes.LPVOID
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]; kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]; kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]; kernel.CloseHandle.restype = wintypes.BOOL
    name, sid = "ElucidaryCrashProbe_" + secrets.token_hex(16), wintypes.LPVOID()
    hr = userenv.CreateAppContainerProfile(name, "Elucidary crash probe", "Disposable synthetic crash fixture", None, 0, ctypes.byref(sid))
    if hr != 0: raise OSError(f"Could not create disposable profile: 0x{hr & 0xffffffff:08x}")
    process, worker_handle, fixture, summary, worker_stopped = None, None, None, None, False
    try:
        with tempfile.TemporaryDirectory(prefix="elucidary-crash-supervisor-", dir=WORKSPACE) as temporary:
            root = Path(temporary).resolve()
            if root.parent != WORKSPACE: raise RuntimeError("Synthetic supervisor escaped workspace")
            ready, fixture_report = root / "ready.json", root / "fixture.json"
            process = subprocess.Popen([sys.executable, str(Path(__file__).with_name("windows_appcontainer_lab.py")),
                                        "--allow-disposable-profile", "--crash-profile", name, "--crash-ready", str(ready)],
                                       close_fds=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            deadline = time.monotonic() + 25
            while not ready.exists() and process.poll() is None and time.monotonic() < deadline: time.sleep(.03)
            if fixture_report.exists(): fixture = disposable_fixture(json.loads(fixture_report.read_text(encoding="utf-8")))
            if not ready.exists():
                if process.poll() is not None:
                    raise RuntimeError("Synthetic parent exited before ready: " + process.stderr.read(4096).decode("utf-8", errors="replace"))
                raise RuntimeError("Synthetic parent did not become ready")
            report = json.loads(ready.read_text(encoding="utf-8"))
            if fixture is None or disposable_fixture(report) != fixture: raise RuntimeError("Synthetic fixture identity mismatch")
            prompt = Path(report["prompt"])
            if prompt != fixture / "exchange" / "input" / "crash-prompt.txt": raise RuntimeError("Invalid synthetic prompt path")
            pid = report.get("worker_pid")
            if type(pid) is not int or pid <= 0: raise RuntimeError("Invalid synthetic worker PID")
            worker_handle = kernel.OpenProcess(0x00100000, False, pid)
            if not worker_handle: raise ctypes.WinError(ctypes.get_last_error())
            process.kill(); process.wait(timeout=10)
            if kernel.WaitForSingleObject(worker_handle, 10000) != 0: raise RuntimeError("AppContainer worker did not terminate")
            worker_stopped = True
            deadline = time.monotonic() + 5
            while prompt.exists() and time.monotonic() < deadline: time.sleep(.03)
            if prompt.exists(): raise RuntimeError("Synthetic delete-on-close prompt remained after termination")
            summary = {"parent_abruptly_stopped": True, "appcontainer_worker_exit_confirmed": True,
                       "synthetic_prompt_removed": True, "secure_erasure_tested": False,
                       "hostile_worker_cleanup_tested": False, "external_network_tested": False}
    finally:
        if process and process.poll() is None:
            process.kill(); process.wait(timeout=10)
        if worker_handle and not worker_stopped:
            worker_stopped = kernel.WaitForSingleObject(worker_handle, 10000) == 0
        if worker_handle: kernel.CloseHandle(worker_handle)
        if process and process.stderr: process.stderr.close()
        try:
            if fixture and fixture.exists():
                if not worker_stopped: raise RuntimeError(f"Synthetic worker termination was not confirmed; fixture left at {fixture}")
                fixture = disposable_fixture({"fixture": str(fixture)})
                shutil.rmtree(fixture)
                if summary is not None: summary["synthetic_fixture_removed"] = True
        finally:
            if sid: advapi.FreeSid(sid)
            hr = userenv.DeleteAppContainerProfile(name)
            if hr != 0: raise RuntimeError(f"Could not remove disposable profile {name}: 0x{hr & 0xffffffff:08x}")
            if summary is not None: summary["profile_delete_api_succeeded"] = True
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-synthetic-crash", action="store_true")
    if not parser.parse_args().allow_synthetic_crash: parser.error("explicit --allow-synthetic-crash is required")
    print(json.dumps(run(), sort_keys=True))


if __name__ == "__main__": main()

"""Opt-in synthetic Windows AppContainer probe; not used by the application.

Creates a randomly named per-user AppContainer profile and deletes only that
profile after its child has exited. Accepts no repository path or source data.
Run only on a suitable Windows host with explicit permission for the profile.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from contextlib import ExitStack
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKSPACE = ROOT.parent.resolve()
sys.path.insert(0, str(ROOT / "src"))

from verifier_lab.ingestion.windows_acl_plan import current_token_identity, exchange_acl_plan, precreated_reply_acl_plan
from verifier_lab.ingestion.windows_reply_guard import ReplyGuardError, close_reply_directory, open_reply_directory, read_reply_from_handle
from verifier_lab.ingestion.common import Limits
from verifier_lab.ingestion.extract import ExtractionFailure
from verifier_lab.ingestion.file_exchange import _decode_reply
from verifier_lab.ingestion.graph_reply_schema import validate_graph_result
from verifier_lab.repository_assistant import prepare_prompt, validate_answer

PYTHON_PROBE_BOOT = r'''
import ctypes, sys
from ctypes import wintypes
advapi = ctypes.WinDLL("advapi32", use_last_error=True)
kernel = ctypes.WinDLL("kernel32", use_last_error=True)
kernel.GetCurrentProcess.restype = wintypes.HANDLE
kernel.CloseHandle.argtypes = [wintypes.HANDLE]
advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
advapi.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
token = wintypes.HANDLE()
if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 8, ctypes.byref(token)): sys.exit(70)
inside, needed = wintypes.DWORD(), wintypes.DWORD()
if not advapi.GetTokenInformation(token, 29, ctypes.byref(inside), ctypes.sizeof(inside), ctypes.byref(needed)) or not inside.value: sys.exit(71)
advapi.GetTokenInformation(token, 30, None, 0, ctypes.byref(needed))
if not needed.value or needed.value > 1048576: sys.exit(72)
buffer = ctypes.create_string_buffer(needed.value)
if not advapi.GetTokenInformation(token, 30, buffer, len(buffer), ctypes.byref(needed)): sys.exit(73)
if ctypes.cast(buffer, ctypes.POINTER(wintypes.DWORD)).contents.value != 0: sys.exit(74)
kernel.CloseHandle(token)
try:
    with open(sys.argv[3], "ab"): pass
except PermissionError: pass
else: sys.exit(75)
try:
    with open(sys.argv[5], "rb"): pass
except PermissionError: pass
else: sys.exit(76)
sys.path.insert(0, sys.argv[1])
from verifier_lab.ingestion.file_exchange import worker_main
sys.exit(worker_main(sys.argv[1:5]))
'''


class _SecurityAttributes(ctypes.Structure):
    _fields_ = [("length", wintypes.DWORD), ("descriptor", wintypes.LPVOID), ("inherit", wintypes.BOOL)]


class _StartupInfo(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("reserved", wintypes.LPWSTR), ("desktop", wintypes.LPWSTR), ("title", wintypes.LPWSTR),
                ("x", wintypes.DWORD), ("y", wintypes.DWORD), ("xsize", wintypes.DWORD), ("ysize", wintypes.DWORD),
                ("xchars", wintypes.DWORD), ("ychars", wintypes.DWORD), ("fill", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("show", wintypes.WORD), ("reserved2_size", wintypes.WORD), ("reserved2", wintypes.LPBYTE),
                ("stdin", wintypes.HANDLE), ("stdout", wintypes.HANDLE), ("stderr", wintypes.HANDLE)]


class _StartupInfoEx(ctypes.Structure):
    _fields_ = [("startup", _StartupInfo), ("attributes", wintypes.LPVOID)]


class _SecurityCapabilities(ctypes.Structure):
    _fields_ = [("sid", wintypes.LPVOID), ("capabilities", wintypes.LPVOID), ("count", wintypes.DWORD), ("reserved", wintypes.DWORD)]


class _ProcessInformation(ctypes.Structure):
    _fields_ = [("process", wintypes.HANDLE), ("thread", wintypes.HANDLE), ("pid", wintypes.DWORD), ("tid", wintypes.DWORD)]


class _BasicLimits(ctypes.Structure):
    _fields_ = [("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong), ("flags", wintypes.DWORD),
                ("min_working", ctypes.c_size_t), ("max_working", ctypes.c_size_t), ("active", wintypes.DWORD),
                ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD), ("scheduling", wintypes.DWORD)]


class _IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [("basic", _BasicLimits), ("io", _IoCounters), ("process_memory", ctypes.c_size_t),
                ("job_memory", ctypes.c_size_t), ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t)]


class SyntheticWorkerTimeout(RuntimeError):
    pass


def synthetic_worker_failure(code):
    """Decode diagnostic exit status without adding paths or fixture content."""
    if 0x19000000 <= code <= 0x1900ffff:
        return f"Synthetic AppContainer probe failed opening the reply (Win32 error {code & 0xffff})"
    if code == 0x19010000:
        return "Synthetic AppContainer probe failed opening the reply (Win32 error exceeds diagnostic range)"
    return f"Synthetic AppContainer probe failed at check {code}"


def run_probe(include_python=False, test_loopback=False, include_model=False, test_timeout=False, test_delete_on_close=False,
              external_profile=None, crash_ready=None, test_precreated_output=False):
    if os.name != "nt": raise OSError("Windows required")
    identity = current_token_identity()
    if identity["is_appcontainer"]:
        raise RuntimeError("Run the synthetic probe from a non-AppContainer host process")
    probe = WORKSPACE / "elucidary-acl-probe.exe"
    if not probe.is_file(): raise RuntimeError("Compile the synthetic probe first")
    userenv = ctypes.WinDLL("userenv", use_last_error=True)
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    userenv.CreateAppContainerProfile.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.LPVOID)]
    userenv.CreateAppContainerProfile.restype = ctypes.c_long
    userenv.DeleteAppContainerProfile.argtypes = [wintypes.LPCWSTR]; userenv.DeleteAppContainerProfile.restype = ctypes.c_long
    userenv.DeriveAppContainerSidFromAppContainerName.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.LPVOID)]
    userenv.DeriveAppContainerSidFromAppContainerName.restype = ctypes.c_long
    advapi.FreeSid.argtypes = [wintypes.LPVOID]; advapi.FreeSid.restype = wintypes.LPVOID
    advapi.ConvertSidToStringSidW.argtypes = [wintypes.LPVOID, ctypes.POINTER(wintypes.LPWSTR)]; advapi.ConvertSidToStringSidW.restype = wintypes.BOOL
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(wintypes.LPVOID), ctypes.POINTER(wintypes.DWORD)]
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [wintypes.HLOCAL]; kernel.LocalFree.restype = wintypes.HLOCAL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]; kernel.CloseHandle.restype = wintypes.BOOL
    kernel.CreateDirectoryW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(_SecurityAttributes)]; kernel.CreateDirectoryW.restype = wintypes.BOOL
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.InitializeProcThreadAttributeList.argtypes = [wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.c_size_t)]
    kernel.InitializeProcThreadAttributeList.restype = wintypes.BOOL
    kernel.UpdateProcThreadAttribute.argtypes = [wintypes.LPVOID, wintypes.DWORD, ctypes.c_size_t, wintypes.LPVOID, ctypes.c_size_t, wintypes.LPVOID, wintypes.LPVOID]
    kernel.UpdateProcThreadAttribute.restype = wintypes.BOOL
    kernel.DeleteProcThreadAttributeList.argtypes = [wintypes.LPVOID]
    kernel.CreateProcessW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.LPVOID, wintypes.LPVOID, wintypes.BOOL, wintypes.DWORD,
                                     wintypes.LPVOID, wintypes.LPCWSTR, ctypes.POINTER(_StartupInfoEx), ctypes.POINTER(_ProcessInformation)]
    kernel.CreateProcessW.restype = wintypes.BOOL
    kernel.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]; kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]; kernel.SetInformationJobObject.restype = wintypes.BOOL
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]; kernel.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]; kernel.TerminateProcess.restype = wintypes.BOOL
    kernel.ResumeThread.argtypes = [wintypes.HANDLE]; kernel.ResumeThread.restype = wintypes.DWORD
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]; kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]; kernel.GetExitCodeProcess.restype = wintypes.BOOL

    def create_dir(path, sddl):
        descriptor = wintypes.LPVOID(); size = wintypes.DWORD()
        if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(descriptor), ctypes.byref(size)):
            raise OSError("Could not compile candidate ACL")
        try:
            attrs = _SecurityAttributes(ctypes.sizeof(_SecurityAttributes), descriptor, False)
            if not kernel.CreateDirectoryW(str(path), ctypes.byref(attrs)): raise ctypes.WinError(ctypes.get_last_error())
        finally: kernel.LocalFree(descriptor)

    def create_file(path, sddl):
        descriptor = wintypes.LPVOID(); size = wintypes.DWORD()
        if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(descriptor), ctypes.byref(size)):
            raise OSError("Could not compile candidate file ACL")
        try:
            attrs = _SecurityAttributes(ctypes.sizeof(_SecurityAttributes), descriptor, False)
            handle = kernel.CreateFileW(str(path), 0x40000000, 0, ctypes.byref(attrs), 1, 0x80, None)
            if handle == ctypes.c_void_p(-1).value: raise ctypes.WinError(ctypes.get_last_error())
            kernel.CloseHandle(handle)
        finally: kernel.LocalFree(descriptor)

    if bool(external_profile) != bool(crash_ready): raise ValueError("Synthetic crash fixture requires both profile and ready path")
    if external_profile and not re.fullmatch(r"ElucidaryCrashProbe_[0-9a-f]{32}", external_profile):
        raise ValueError("Invalid synthetic crash profile")
    if crash_ready:
        crash_ready = Path(crash_ready).resolve()
        if crash_ready.name != "ready.json" or crash_ready.parent.parent != WORKSPACE or not crash_ready.parent.name.startswith("elucidary-crash-supervisor-"):
            raise ValueError("Invalid synthetic crash ready path")
    name = external_profile or "ElucidaryProbe_" + secrets.token_hex(16)
    sid = wintypes.LPVOID(); created = False; listener = None; summary = None
    if external_profile:
        hr = userenv.DeriveAppContainerSidFromAppContainerName(name, ctypes.byref(sid))
        if hr != 0: raise OSError(f"Could not derive synthetic profile SID: 0x{hr & 0xffffffff:08x}")
    else:
        hr = userenv.CreateAppContainerProfile(name, "Elucidary synthetic probe", "Disposable synthetic isolation test", None, 0, ctypes.byref(sid))
        if hr != 0: raise OSError(f"Could not create disposable AppContainer profile: 0x{hr & 0xffffffff:08x}")
        created = True
    stage = "read profile identity"
    try:
        sid_text = wintypes.LPWSTR()
        if not advapi.ConvertSidToStringSidW(sid, ctypes.byref(sid_text)): raise ctypes.WinError(ctypes.get_last_error())
        try:
            plan = exchange_acl_plan(identity["user_sid"], sid_text.value)
            locked_plan = precreated_reply_acl_plan(identity["user_sid"], sid_text.value) if test_precreated_output else None
        finally: kernel.LocalFree(ctypes.cast(sid_text, wintypes.HLOCAL))
        with tempfile.TemporaryDirectory(prefix="elucidary-appcontainer-probe-", dir=WORKSPACE) as temporary, ExitStack() as handles:
            fixture = Path(temporary).resolve()
            if fixture.parent != WORKSPACE: raise RuntimeError("Synthetic fixture escaped the workspace")
            if crash_ready:
                crash_ready.with_name("fixture.json").write_text(json.dumps({"fixture": str(fixture)}), encoding="utf-8")
            root = fixture / "exchange"; incoming, outgoing = root / "input", root / "output"
            stage = "create candidate directories"
            create_dir(root, plan["root"]); create_dir(incoming, plan["input"]); create_dir(outgoing, plan["output"])
            output_handle = open_reply_directory(outgoing)
            handles.callback(close_reply_directory, output_handle)
            worker = incoming / "probe.exe"; shutil.copyfile(probe, worker)
            source, outside, reply = incoming / "source.txt", root / "outside.txt", outgoing / "result.json"
            source.write_bytes(b"synthetic input"); outside.write_bytes(b"synthetic outside sentinel")
            arguments = [source, outside, reply]
            if test_loopback:
                stage = "prepare local-only test listener"
                listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                listener.bind(("127.0.0.1", 0)); listener.listen(1); listener.settimeout(2)
                with socket.create_connection(listener.getsockname(), timeout=2):
                    accepted, _ = listener.accept(); accepted.close()
                arguments.append(str(listener.getsockname()[1]))
            def launch(executable, arguments, memory, timeout_ms=15000, on_resumed=None):
                nonlocal stage
                stage = "construct process attributes"
                length = ctypes.c_size_t()
                kernel.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(length))
                if not length.value: raise ctypes.WinError(ctypes.get_last_error())
                attributes = ctypes.create_string_buffer(length.value)
                if not kernel.InitializeProcThreadAttributeList(attributes, 1, 0, ctypes.byref(length)): raise ctypes.WinError(ctypes.get_last_error())
                process = _ProcessInformation(); job = None
                try:
                    caps = _SecurityCapabilities(sid, None, 0, 0)
                    if not kernel.UpdateProcThreadAttribute(attributes, 0, 0x00020009, ctypes.byref(caps), ctypes.sizeof(caps), None, None):
                        raise ctypes.WinError(ctypes.get_last_error())
                    startup = _StartupInfoEx(); startup.startup.cb = ctypes.sizeof(_StartupInfoEx); startup.attributes = ctypes.cast(attributes, wintypes.LPVOID)
                    command = ctypes.create_unicode_buffer(subprocess.list2cmdline([str(executable), *map(str, arguments)]))
                    environment = {"SystemRoot": os.environ.get("SystemRoot", r"C:\Windows"), "WINDIR": os.environ.get("WINDIR", r"C:\Windows"),
                                   "USERPROFILE": str(root), "LOCALAPPDATA": str(root), "TEMP": str(outgoing), "TMP": str(outgoing)}
                    env = ctypes.create_unicode_buffer("\0".join(f"{key}={value}" for key, value in sorted(environment.items())) + "\0\0")
                    flags = 0x00080000 | 0x00000004 | 0x00000400 | 0x08000000
                    stage = "start AppContainer process"
                    if not kernel.CreateProcessW(str(executable), command, None, None, False, flags, env, str(incoming), ctypes.byref(startup), ctypes.byref(process)):
                        raise ctypes.WinError(ctypes.get_last_error())
                    stage = "configure process job limits"
                    job = kernel.CreateJobObjectW(None, None)
                    if not job: raise ctypes.WinError(ctypes.get_last_error())
                    limits = _ExtendedLimits(); limits.basic.flags = 0x2000 | 0x100 | 0x8; limits.basic.active = 1; limits.process_memory = memory
                    if not kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)): raise ctypes.WinError(ctypes.get_last_error())
                    if not kernel.AssignProcessToJobObject(job, process.process): raise ctypes.WinError(ctypes.get_last_error())
                    stage = "resume and wait for synthetic worker"
                    if kernel.ResumeThread(process.thread) == 0xffffffff: raise ctypes.WinError(ctypes.get_last_error())
                    if on_resumed: on_resumed(process.pid)
                    waited = kernel.WaitForSingleObject(process.process, timeout_ms)
                    if waited == 0x102: raise SyntheticWorkerTimeout("Synthetic AppContainer probe timed out")
                    if waited != 0: raise RuntimeError("Synthetic AppContainer process wait failed")
                    code = wintypes.DWORD()
                    if not kernel.GetExitCodeProcess(process.process, ctypes.byref(code)): raise ctypes.WinError(ctypes.get_last_error())
                    if code.value != 0: raise RuntimeError(synthetic_worker_failure(code.value))
                finally:
                    cleanup_error = None
                    if process.process:
                        waited = kernel.WaitForSingleObject(process.process, 0)
                        if waited == 0x102:
                            if not kernel.TerminateProcess(process.process, 1) and kernel.WaitForSingleObject(process.process, 0) != 0:
                                cleanup_error = "Could not terminate synthetic worker"
                            if kernel.WaitForSingleObject(process.process, 5000) != 0:
                                cleanup_error = "Synthetic worker termination was not confirmed"
                        elif waited != 0:
                            cleanup_error = "Could not inspect synthetic worker state"
                    if job: kernel.CloseHandle(job)
                    if process.thread: kernel.CloseHandle(process.thread)
                    if process.process: kernel.CloseHandle(process.process)
                    kernel.DeleteProcThreadAttributeList(attributes)
                    if cleanup_error: raise RuntimeError(cleanup_error)

            launch(worker, arguments, 128 * 1024 * 1024)
            stage = "read guarded synthetic reply"
            result = json.loads(read_reply_from_handle(output_handle, 1024))
            if test_loopback:
                if result not in ({"synthetic_probe": True, "loopback_result": "os_access_denied"}, {"synthetic_probe": True, "loopback_result": "timeout"}):
                    raise RuntimeError("Unexpected synthetic loopback result")
                listener.settimeout(.1)
                try:
                    accepted, _ = listener.accept(); accepted.close()
                    raise RuntimeError("The synthetic worker connected to the test listener")
                except socket.timeout:
                    pass
            elif result != {"synthetic_probe": True}: raise RuntimeError("Unexpected synthetic reply")
            summary = {"appcontainer": True, "capabilities": 0, "input_read": True, "input_write_denied": True,
                       "outside_read_denied": True, "child_process_denied": True, "output_written": True,
                       "loopback_result": result.get("loopback_result"), "external_network_tested": False}
            if include_python:
                stage = "prepare synthetic parser runtime"
                bundle = WORKSPACE / "Elucidary-Desktop-v3"
                if not (bundle / "runtime" / "python.exe").is_file() or not (bundle / "app" / "verifier_lab").is_dir():
                    raise RuntimeError("Built desktop runtime is unavailable")
                shutil.copytree(bundle / "runtime", incoming / "runtime", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
                shutil.copytree(bundle / "app", incoming / "app", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
                shutil.copyfile(ROOT / "src" / "verifier_lab" / "ingestion" / "file_exchange.py", incoming / "app" / "verifier_lab" / "ingestion" / "file_exchange.py")
                reply.unlink()
                samples = (("repository-python", "def f(x): return x\n", "fixture.py"),
                           ("repository-javascript", "function f(x) { return x; }\n", "fixture.js"),
                           ("repository-typescript", "function f(x: number): number { return x; }\n", "fixture.ts"),
                           ("repository-cpp", "int f(int x) { return x; }\n", "fixture.cpp"))
                checked = []
                for fmt, text, filename in samples:
                    stage = "prepare " + fmt + " fixture"
                    fixture_source = incoming / filename; fixture_source.write_bytes(text.encode("utf-8"))
                    request = json.dumps({"format": fmt, "dialect": filename, "limits": Limits(isolation="portable").to_dict()}, ensure_ascii=True)
                    launch(incoming / "runtime" / "python.exe", ["-E", "-s", "-B", "-c", PYTHON_PROBE_BOOT, incoming / "app", request, fixture_source, reply, outside], 512 * 1024 * 1024)
                    stage = "read " + fmt + " reply"
                    parsed = validate_graph_result(_decode_reply(read_reply_from_handle(output_handle, 1_000_000)), fmt, text.encode("utf-8"), filename)
                    if not any(n.get("name") == "f" for n in parsed.get("nodes", [])):
                        raise RuntimeError("Synthetic parser result is missing the expected function")
                    checked.append(fmt); reply.unlink()
                if outside.read_bytes() != b"synthetic outside sentinel": raise RuntimeError("Synthetic outside sentinel changed")
                summary["synthetic_parser_formats"] = checked
                summary["each_parser_checked_token_and_file_denials"] = True
            if include_model or test_timeout or test_delete_on_close or crash_ready or test_precreated_output:
                stage = "prepare fixed synthetic model fixture"
                mock_binary = WORKSPACE / "elucidary-mock-model.exe"
                if not mock_binary.is_file(): raise RuntimeError("Compile the fixed synthetic mock model first")
                mock = incoming / "mock-model.exe"; shutil.copyfile(mock_binary, mock)
                evidence = [{"citation": {"path": "synthetic.py", "line": 1, "text": "def f(x): return x", "evidence": "static"}}]
                prompt, count = prepare_prompt(evidence, "returns", "beginner")
                prompt_file = incoming / "synthetic-prompt.txt"; prompt_file.write_text(prompt, encoding="utf-8")
                if reply.exists(): reply.unlink()
                launch(mock, [prompt_file, outside, reply, "1"], 128 * 1024 * 1024)
                stage = "validate fixed synthetic model reply"
                accepted = validate_answer(read_reply_from_handle(output_handle, 1024).decode("utf-8"), evidence, count)
                if accepted["citations"][0]["path"] != "synthetic.py": raise RuntimeError("Synthetic citation was not attached")
                reply.unlink()
                launch(mock, [prompt_file, outside, reply, "2"], 128 * 1024 * 1024)
                try:
                    validate_answer(read_reply_from_handle(output_handle, 1024).decode("utf-8"), evidence, count)
                except ValueError:
                    summary["mock_model_citation_validation"] = True
                else:
                    raise RuntimeError("Invalid synthetic model citation was accepted")
                reply.unlink()
                launch(mock, [prompt_file, outside, reply, "duplicate"], 128 * 1024 * 1024)
                try:
                    _decode_reply(read_reply_from_handle(output_handle, 1024))
                except ExtractionFailure as exc:
                    if exc.code != "worker_protocol_error": raise
                else:
                    raise RuntimeError("Duplicate-key synthetic worker reply was accepted")
                reply.unlink()
                launch(mock, [prompt_file, outside, reply, "oversized"], 128 * 1024 * 1024)
                try:
                    read_reply_from_handle(output_handle, 1024)
                except ReplyGuardError as exc:
                    if exc.code != "output_limit": raise
                else:
                    raise RuntimeError("Oversized synthetic worker reply was accepted")
                summary["malformed_and_oversized_replies_rejected"] = True
                summary["real_model_tested"] = False
                if test_timeout:
                    reply.unlink()
                    try:
                        launch(mock, [prompt_file, outside, reply, "stall"], 128 * 1024 * 1024, timeout_ms=1500)
                    except SyntheticWorkerTimeout:
                        if not reply.is_file(): raise RuntimeError("Synthetic stalled worker did not create its fixture")
                        summary["timed_out_worker_termination_confirmed"] = True
                    else:
                        raise RuntimeError("Stalled synthetic worker unexpectedly completed")
                if test_delete_on_close:
                    if reply.exists(): reply.unlink()
                    temporary_prompt = incoming / "delete-on-close-prompt.txt"
                    temporary_prompt.write_text(prompt, encoding="utf-8")
                    parent_handle = kernel.CreateFileW(str(temporary_prompt), 0x80000000 | 0x00010000, 7, None, 3, 0x04000000 | 0x100, None)
                    if parent_handle == ctypes.c_void_p(-1).value: raise ctypes.WinError(ctypes.get_last_error())
                    try:
                        launch(mock, [temporary_prompt, outside, reply, "1"], 128 * 1024 * 1024)
                        validate_answer(read_reply_from_handle(output_handle, 1024).decode("utf-8"), evidence, count)
                    finally:
                        kernel.CloseHandle(parent_handle)
                    if temporary_prompt.exists(): raise RuntimeError("Delete-on-close synthetic prompt was not removed")
                    summary["appcontainer_read_delete_on_close_fixture"] = True
                if test_precreated_output:
                    stage = "create synthetic traverse-only output and precreated reply"
                    locked_output = root / "locked-output"
                    create_dir(locked_output, locked_plan["directory"])
                    locked_reply = locked_output / "result.json"
                    create_file(locked_reply, locked_plan["reply"])
                    locked_handle = open_reply_directory(locked_output)
                    handles.callback(close_reply_directory, locked_handle)
                    launch(mock, [prompt_file, outside, locked_reply, "precreated"], 128 * 1024 * 1024)
                    result = validate_answer(read_reply_from_handle(locked_handle, 1024).decode("utf-8"), evidence, count)
                    if result["citations"][0]["path"] != "synthetic.py" or (locked_output / "extra.tmp").exists():
                        raise RuntimeError("Synthetic restricted output result was not as expected")
                    summary["precreated_reply_written_and_create_delete_denied"] = True
                if crash_ready:
                    if reply.exists(): reply.unlink()
                    crash_prompt = incoming / "crash-prompt.txt"
                    crash_prompt.write_text(prompt, encoding="utf-8")
                    parent_handle = kernel.CreateFileW(str(crash_prompt), 0x80000000 | 0x00010000, 7, None, 3, 0x04000000 | 0x100, None)
                    if parent_handle == ctypes.c_void_p(-1).value: raise ctypes.WinError(ctypes.get_last_error())
                    try:
                        def signal_worker(pid):
                            deadline = time.monotonic() + 10
                            while not reply.exists() and time.monotonic() < deadline: time.sleep(.03)
                            if not reply.exists(): raise RuntimeError("Synthetic stalled AppContainer worker did not create its reply")
                            temporary_ready = crash_ready.with_suffix(".tmp")
                            temporary_ready.write_text(json.dumps({"worker_pid": pid, "fixture": str(fixture), "prompt": str(crash_prompt)}), encoding="utf-8")
                            os.replace(temporary_ready, crash_ready)
                        launch(mock, [crash_prompt, outside, reply, "stall"], 128 * 1024 * 1024, on_resumed=signal_worker)
                        raise RuntimeError("Synthetic crash supervisor did not stop its test parent")
                    finally:
                        kernel.CloseHandle(parent_handle)
        if fixture.exists(): raise RuntimeError("Synthetic temporary fixture was not removed")
        summary["synthetic_temporary_fixture_removed"] = True
        return summary
    except Exception as exc:
        raise RuntimeError(f"{stage}: {exc}") from exc
    finally:
        if listener: listener.close()
        if sid: advapi.FreeSid(sid)
        if created:
            result = userenv.DeleteAppContainerProfile(name)
            if result != 0: raise RuntimeError(f"Could not remove disposable profile {name}: 0x{result & 0xffffffff:08x}")
            if summary is not None: summary["profile_delete_api_succeeded"] = True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-disposable-profile", action="store_true")
    parser.add_argument("--include-python-fixtures", action="store_true")
    parser.add_argument("--test-loopback", action="store_true", help="test only against a temporary listener on 127.0.0.1")
    parser.add_argument("--include-model-fixture", action="store_true", help="use a fixed mock, not a language model")
    parser.add_argument("--test-worker-timeout", action="store_true", help="stop a fixed synthetic stalled worker and check scratch cleanup")
    parser.add_argument("--test-delete-on-close", action="store_true", help="read a fixed delete-on-close prompt from an AppContainer")
    parser.add_argument("--test-precreated-output", action="store_true", help="test a traverse-only output directory and fixed reply file")
    parser.add_argument("--crash-profile", help=argparse.SUPPRESS)
    parser.add_argument("--crash-ready", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not args.allow_disposable_profile:
        parser.error("explicit --allow-disposable-profile is required")
    try:
        print(json.dumps(run_probe(args.include_python_fixtures, args.test_loopback, args.include_model_fixture, args.test_worker_timeout,
                                   args.test_delete_on_close, args.crash_profile, args.crash_ready, args.test_precreated_output), sort_keys=True))
        return 0
    except Exception as exc:
        print(f"Synthetic AppContainer probe failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

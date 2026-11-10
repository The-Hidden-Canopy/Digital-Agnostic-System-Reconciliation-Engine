"""Experimental ACL policy construction; it does not apply permissions or create a sandbox.

Only synthetic SIDs are used in tests. The policy and its enforcement still
require review and validation on a suitable non-AppContainer Windows host.
"""
from __future__ import annotations

import ctypes
import json
import os
import secrets
import tempfile
from ctypes import wintypes
from pathlib import Path


def _canonical_sid(value, prefix, count):
    if not isinstance(value, str):
        raise ValueError("SID must be text")
    parts = value.split("-")
    if parts[:len(prefix)] != prefix or len(parts) != len(prefix) + count:
        raise ValueError("Unsupported SID shape")
    values = parts[len(prefix):]
    if any(not value.isascii() or not value.isdecimal() or len(value) > 10 for value in values):
        raise ValueError("Invalid SID component")
    numbers = [int(value) for value in values]
    if any(number > 0xffffffff for number in numbers):
        raise ValueError("SID component out of range")
    return "-".join(prefix + [str(number) for number in numbers])


def exchange_acl_plan(user_sid, worker_sid, parent_app_sid=None):
    """Construct candidate protected DACLs for distinct input and output folders.

    Root permits worker traversal, input permits read/execute, and output permits
    read/write/execute with a low-integrity label. This is a policy proposal only.
    """
    user = _canonical_sid(user_sid, ["S", "1", "5", "21"], 4)
    worker = _canonical_sid(worker_sid, ["S", "1", "15", "2"], 7)
    parent = _canonical_sid(parent_app_sid, ["S", "1", "15", "2"], 7) if parent_app_sid is not None else None
    if parent == worker:
        raise ValueError("Worker and parent AppContainer SIDs must differ")
    owner_aces = f"(A;OICI;FA;;;SY)(A;OICI;FA;;;{user})" + (f"(A;OICI;FA;;;{parent})" if parent else "")
    return {
        "root": f"D:P{owner_aces}(A;;GX;;;{worker})",
        "input": f"D:P{owner_aces}(A;OICI;GRGX;;;{worker})",
        "output": f"D:P{owner_aces}(A;OICI;GRGWGX;;;{worker})S:(ML;OICI;NW;;;LW)",
    }


def precreated_reply_acl_plan(user_sid, worker_sid, parent_app_sid=None):
    """Candidate traverse-only directory and writable existing reply file ACLs."""
    user = _canonical_sid(user_sid, ["S", "1", "5", "21"], 4)
    worker = _canonical_sid(worker_sid, ["S", "1", "15", "2"], 7)
    parent = _canonical_sid(parent_app_sid, ["S", "1", "15", "2"], 7) if parent_app_sid is not None else None
    if parent == worker:
        raise ValueError("Worker and parent AppContainer SIDs must differ")
    owner_aces = f"(A;OICI;FA;;;SY)(A;OICI;FA;;;{user})" + (f"(A;OICI;FA;;;{parent})" if parent else "")
    return {"directory": f"D:P{owner_aces}(A;;GX;;;{worker})",
            "reply": f"D:P{owner_aces}(A;;GW;;;{worker})S:(ML;;NW;;;LW)"}


def current_token_identity():
    """Read the current process user and AppContainer SID; creates no profile."""
    if os.name != "nt":
        raise OSError("Windows token inspection unavailable")
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]; kernel.CloseHandle.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [wintypes.HLOCAL]; kernel.LocalFree.restype = wintypes.HLOCAL
    advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi.OpenProcessToken.restype = wintypes.BOOL
    advapi.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    advapi.GetTokenInformation.restype = wintypes.BOOL
    advapi.ConvertSidToStringSidW.argtypes = [wintypes.LPVOID, ctypes.POINTER(wintypes.LPWSTR)]
    advapi.ConvertSidToStringSidW.restype = wintypes.BOOL
    token = wintypes.HANDLE()
    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
        raise OSError("Could not inspect the current Windows token")

    def query(which):
        length = wintypes.DWORD()
        advapi.GetTokenInformation(token, which, None, 0, ctypes.byref(length))
        if not length.value or length.value > 1024 * 1024:
            raise OSError("Could not determine token information size")
        buffer = ctypes.create_string_buffer(length.value)
        if not advapi.GetTokenInformation(token, which, buffer, length.value, ctypes.byref(length)):
            raise OSError("Could not inspect token information")
        return buffer

    def sid_from(buffer):
        sid = ctypes.cast(buffer, ctypes.POINTER(wintypes.LPVOID)).contents.value
        if not sid:
            raise OSError("Windows returned an empty SID")
        string = wintypes.LPWSTR()
        if not advapi.ConvertSidToStringSidW(sid, ctypes.byref(string)):
            raise OSError("Could not read a token SID")
        try:
            return string.value
        finally:
            kernel.LocalFree(ctypes.cast(string, wintypes.HLOCAL))

    try:
        user = sid_from(query(1))  # TokenUser
        is_app = bool(ctypes.cast(query(29), ctypes.POINTER(wintypes.DWORD)).contents.value)  # TokenIsAppContainer
        app = sid_from(query(31)) if is_app else None  # TokenAppContainerSid
        return {"user_sid": user, "appcontainer_sid": app, "is_appcontainer": is_app}
    finally:
        kernel.CloseHandle(token)


def synthetic_acl_exchange():
    """Exercise candidate ACLs and file transport with a fixed synthetic fixture only.

    The worker is a normal portable subprocess, not an AppContainer. This helper
    accepts no source or path arguments and is excluded from the desktop bundle.
    """
    if os.name != "nt":
        raise OSError("Windows ACL experiment unavailable")
    from .common import Limits
    from .file_exchange import _run_file_worker
    from .windows_reply_guard import read_reply

    class SecurityAttributes(ctypes.Structure):
        _fields_ = [("length", wintypes.DWORD), ("descriptor", wintypes.LPVOID), ("inherit", wintypes.BOOL)]

    advapi = ctypes.WinDLL("advapi32", use_last_error=True); kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                                                                             ctypes.POINTER(wintypes.LPVOID), ctypes.POINTER(wintypes.DWORD)]
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    kernel.CreateDirectoryW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(SecurityAttributes)]; kernel.CreateDirectoryW.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [wintypes.HLOCAL]; kernel.LocalFree.restype = wintypes.HLOCAL

    def create(path, sddl):
        descriptor = wintypes.LPVOID(); length = wintypes.DWORD()
        if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(descriptor), ctypes.byref(length)):
            raise OSError("Windows rejected a candidate security descriptor")
        try:
            attributes = SecurityAttributes(ctypes.sizeof(SecurityAttributes), descriptor, False)
            if not kernel.CreateDirectoryW(str(path), ctypes.byref(attributes)):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            kernel.LocalFree(descriptor)

    identity = current_token_identity()
    worker_sid = "S-1-15-2-" + "-".join(str(secrets.randbits(32)) for _ in range(7))
    plan = exchange_acl_plan(identity["user_sid"], worker_sid, identity["appcontainer_sid"])
    temporary_root = Path(tempfile.gettempdir()).resolve()
    with tempfile.TemporaryDirectory(prefix="elucidary-acl-exchange-", dir=temporary_root) as temporary:
        fixture = Path(temporary).resolve()
        if fixture.parent != temporary_root:
            raise OSError("Synthetic exchange escaped the temporary directory")
        root = fixture / "exchange"; incoming, outgoing = root / "input", root / "output"
        create(root, plan["root"]); create(incoming, plan["input"]); create(outgoing, plan["output"])
        marker, input_file, output_file = root / "fixture_executed", incoming / "source.py", outgoing / "result.json"
        input_file.write_text(f"from pathlib import Path\nPath({str(marker)!r}).touch()\ndef example(): return 1\n", encoding="utf-8")
        limits = Limits(isolation="portable")
        request = json.dumps({"format": "repository-python", "dialect": "synthetic.py", "limits": limits.to_dict()}, ensure_ascii=True)
        _run_file_worker(request, input_file, output_file, limits, root)
        result = json.loads(read_reply(outgoing, 1_000_000))
        return {"parser_ok": result.get("ok") is True,
                "function_found": any(n.get("name") == "example" for n in result.get("extraction", {}).get("nodes", [])),
                "fixture_executed": marker.exists(), "worker_isolated": False, "caller_is_appcontainer": identity["is_appcontainer"]}


def validate_descriptor_syntax(sddl):
    """Ask Windows to parse a descriptor in memory; this does not apply it to a file."""
    if os.name != "nt":
        raise OSError("Windows security descriptor parser unavailable")
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                                                                             ctypes.POINTER(wintypes.LPVOID), ctypes.POINTER(wintypes.DWORD)]
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    advapi.GetSecurityDescriptorControl.argtypes = [wintypes.LPVOID, ctypes.POINTER(wintypes.WORD), ctypes.POINTER(wintypes.DWORD)]
    advapi.GetSecurityDescriptorControl.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [wintypes.HLOCAL]; kernel.LocalFree.restype = wintypes.HLOCAL
    descriptor = wintypes.LPVOID(); size = wintypes.DWORD()
    if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(descriptor), ctypes.byref(size)):
        raise OSError("Windows rejected the security descriptor")
    try:
        control, revision = wintypes.WORD(), wintypes.DWORD()
        if not advapi.GetSecurityDescriptorControl(descriptor, ctypes.byref(control), ctypes.byref(revision)):
            raise OSError("Windows could not inspect the security descriptor")
        return {"dacl_present": bool(control.value & 0x0004), "dacl_protected": bool(control.value & 0x1000),
                "sacl_present": bool(control.value & 0x0010)}
    finally:
        kernel.LocalFree(descriptor)

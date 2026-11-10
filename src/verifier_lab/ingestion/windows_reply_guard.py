"""Experimental Windows reply-file checks; not an AppContainer or security claim.

This module is used only by the synthetic portable transport prototype. It does
not create ACLs, validate an isolated worker, or authorize private source data.
"""
from __future__ import annotations

import ctypes
import ntpath
import os
from ctypes import wintypes
from pathlib import Path

FILE_ATTRIBUTE_DIRECTORY = 0x10
FILE_ATTRIBUTE_REPARSE_POINT = 0x400
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
FILE_SHARE_READ = 1
FILE_SHARE_WRITE = 2
GENERIC_READ = 0x80000000
FILE_READ_ATTRIBUTES = 0x80
OPEN_EXISTING = 3
FILE_TYPE_DISK = 1


class ReplyGuardError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


class _Info(ctypes.Structure):
    _fields_ = [("attributes", wintypes.DWORD), ("created", wintypes.FILETIME), ("accessed", wintypes.FILETIME),
                ("written", wintypes.FILETIME), ("volume", wintypes.DWORD), ("size_high", wintypes.DWORD),
                ("size_low", wintypes.DWORD), ("links", wintypes.DWORD), ("index_high", wintypes.DWORD), ("index_low", wintypes.DWORD)]


class _UnicodeString(ctypes.Structure):
    _fields_ = [("length", wintypes.USHORT), ("maximum_length", wintypes.USHORT), ("buffer", wintypes.LPWSTR)]


class _ObjectAttributes(ctypes.Structure):
    _fields_ = [("length", wintypes.ULONG), ("root", wintypes.HANDLE), ("name", ctypes.POINTER(_UnicodeString)),
                ("attributes", wintypes.ULONG), ("security", wintypes.LPVOID), ("quality", wintypes.LPVOID)]


class _IoStatusBlock(ctypes.Structure):
    _fields_ = [("status", ctypes.c_void_p), ("information", ctypes.c_size_t)]


def _open_relative_reply(directory):
    """Open only result.json relative to the already-open directory handle."""
    ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
    ntdll.NtCreateFile.argtypes = [ctypes.POINTER(wintypes.HANDLE), wintypes.ULONG, ctypes.POINTER(_ObjectAttributes),
                                  ctypes.POINTER(_IoStatusBlock), wintypes.LPVOID, wintypes.ULONG, wintypes.ULONG,
                                  wintypes.ULONG, wintypes.ULONG, wintypes.LPVOID, wintypes.ULONG]
    ntdll.NtCreateFile.restype = ctypes.c_long
    name_buffer = ctypes.create_unicode_buffer("result.json")
    byte_length = len("result.json".encode("utf-16-le"))
    name = _UnicodeString(byte_length, byte_length + 2, ctypes.cast(name_buffer, wintypes.LPWSTR))
    attrs = _ObjectAttributes(ctypes.sizeof(_ObjectAttributes), directory, ctypes.pointer(name), 0, None, None)
    status, handle = _IoStatusBlock(), wintypes.HANDLE()
    # Synchronous read; no write/delete sharing; do not follow the final reparse point.
    code = ntdll.NtCreateFile(ctypes.byref(handle), GENERIC_READ | 0x00100000, ctypes.byref(attrs), ctypes.byref(status),
                              None, 0, FILE_SHARE_READ, 1, 0x00200000 | 0x00000040 | 0x00000020, None, 0)
    if code != 0 or not handle.value:
        if handle.value:
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel.CloseHandle(handle)
        raise ReplyGuardError("worker_protocol_error")
    return handle.value


def _validate(info, *, directory, limit=0):
    if info.attributes & FILE_ATTRIBUTE_REPARSE_POINT:
        raise ReplyGuardError("worker_protocol_error")
    if bool(info.attributes & FILE_ATTRIBUTE_DIRECTORY) != directory:
        raise ReplyGuardError("worker_protocol_error")
    if not directory:
        if info.links != 1:
            raise ReplyGuardError("worker_protocol_error")
        if (info.size_high << 32 | info.size_low) > limit:
            raise ReplyGuardError("output_limit")


def _same_parent(directory, child):
    return ntpath.normcase(ntpath.normpath(directory)) == ntpath.normcase(ntpath.dirname(ntpath.normpath(child)))


def open_reply_directory(root: Path):
    """Open a synthetic reply directory to hold while a worker is running."""
    if os.name != "nt" or not root.is_absolute():
        raise ReplyGuardError("worker_protocol_error")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                  wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.GetFileType.argtypes = [wintypes.HANDLE]; kernel.GetFileType.restype = wintypes.DWORD
    kernel.GetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.POINTER(_Info)]
    kernel.GetFileInformationByHandle.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]; kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.CreateFileW(str(root), FILE_READ_ATTRIBUTES, FILE_SHARE_READ | FILE_SHARE_WRITE, None,
                                OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ReplyGuardError("worker_protocol_error")
    try:
        info = _Info()
        if kernel.GetFileType(handle) != FILE_TYPE_DISK or not kernel.GetFileInformationByHandle(handle, ctypes.byref(info)):
            raise ReplyGuardError("worker_protocol_error")
        _validate(info, directory=True)
    except Exception:
        kernel.CloseHandle(handle)
        raise
    return handle


def close_reply_directory(handle):
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]; kernel.CloseHandle.restype = wintypes.BOOL
    if not kernel.CloseHandle(handle): raise ReplyGuardError("worker_protocol_error")


def read_reply_from_handle(directory, limit: int) -> bytes:
    """Read only the fixed reply under an already-open directory handle."""
    if os.name != "nt" or not directory or type(limit) is not int or limit < 0:
        raise ReplyGuardError("worker_protocol_error")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.POINTER(_Info)]
    kernel.GetFileInformationByHandle.restype = wintypes.BOOL
    kernel.GetFileType.argtypes = [wintypes.HANDLE]; kernel.GetFileType.restype = wintypes.DWORD
    kernel.GetFinalPathNameByHandleW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
    kernel.GetFinalPathNameByHandleW.restype = wintypes.DWORD
    kernel.ReadFile.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
    kernel.ReadFile.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]; kernel.CloseHandle.restype = wintypes.BOOL
    def info_for(handle):
        info = _Info()
        if kernel.GetFileType(handle) != FILE_TYPE_DISK or not kernel.GetFileInformationByHandle(handle, ctypes.byref(info)):
            raise ReplyGuardError("worker_protocol_error")
        return info

    def final_path(handle):
        size = kernel.GetFinalPathNameByHandleW(handle, None, 0, 0)
        if not size or size > 32768:
            raise ReplyGuardError("worker_protocol_error")
        buffer = ctypes.create_unicode_buffer(size + 1)
        used = kernel.GetFinalPathNameByHandleW(handle, buffer, len(buffer), 0)
        if not used or used >= len(buffer):
            raise ReplyGuardError("worker_protocol_error")
        return buffer.value

    _validate(info_for(directory), directory=True)
    result = _open_relative_reply(directory)
    try:
        _validate(info_for(result), directory=False, limit=limit)
        if not _same_parent(final_path(directory), final_path(result)):
            raise ReplyGuardError("worker_protocol_error")
        chunks, total = [], 0
        while True:
            size = min(65536, limit + 1 - total)
            if size <= 0:
                raise ReplyGuardError("output_limit")
            buffer = ctypes.create_string_buffer(size); read = wintypes.DWORD()
            if not kernel.ReadFile(result, buffer, size, ctypes.byref(read), None):
                raise ReplyGuardError("worker_protocol_error")
            if not read.value:
                break
            total += read.value
            if total > limit:
                raise ReplyGuardError("output_limit")
            chunks.append(buffer.raw[:read.value])
        _validate(info_for(result), directory=False, limit=limit)
        return b"".join(chunks)
    finally:
        kernel.CloseHandle(result)


def read_reply(root: Path, limit: int) -> bytes:
    """Open a directory by path, then read its fixed child without following it."""
    directory = open_reply_directory(root)
    try:
        return read_reply_from_handle(directory, limit)
    finally:
        close_reply_directory(directory)

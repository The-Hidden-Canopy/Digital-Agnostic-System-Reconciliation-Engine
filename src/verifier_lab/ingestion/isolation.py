"""Linux parser boundary: minimal read-only filesystem, private namespaces and syscall denial.

No document field controls the executable or mount list. Portable mode is an explicit trusted
configuration for development; it is not advertised as a hostile-native-code sandbox.
"""
from __future__ import annotations

import ctypes
import errno
import os
from pathlib import Path
import shutil
import sys


class IsolationUnavailable(RuntimeError):
    pass


def command(argv: list[str], package_root: str, mode: str) -> list[str]:
    if mode == 'portable':
        return argv
    if mode != 'required' or sys.platform != 'linux':
        raise IsolationUnavailable('required parser isolation needs Linux and bubblewrap')
    bwrap = shutil.which('bwrap')
    if not bwrap:
        raise IsolationUnavailable('install bubblewrap for required parser isolation')
    # Bind only the interpreter, its dependencies, installed packages and this package's code.
    # The application workspace, source documents, credentials and host /tmp are not mounted.
    roots = {Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve(), Path(package_root).resolve()}
    libraries = [Path(p) for p in ('/usr/lib', '/lib', '/lib64') if Path(p).exists()]
    args = [bwrap, '--unshare-all', '--die-with-parent', '--new-session', '--cap-drop', 'ALL',
            '--clearenv', '--setenv', 'LANG', 'C.UTF-8']
    for root in sorted(roots, key=str):
        args += ['--ro-bind', str(root), str(root)]
    for path in libraries:
        args += ['--ro-bind', str(path), str(path)]
    if Path('/etc/ld.so.cache').exists():
        args += ['--ro-bind', '/etc/ld.so.cache', '/etc/ld.so.cache']
    args += ['--proc', '/proc', '--dev', '/dev', '--dir', '/work', '--chdir', '/work',
             '--size', '16777216', '--tmpfs', '/tmp', '--remount-ro', '/', '--', *argv]
    return args


def deny_syscalls() -> None:
    """Deny native networking, new processes and namespace/privilege escape after bootstrap."""
    try:
        library = ctypes.CDLL('libseccomp.so.2', use_errno=True)
    except OSError as exc:
        raise IsolationUnavailable('libseccomp.so.2 is required for parser syscall isolation') from exc
    library.seccomp_init.argtypes = [ctypes.c_uint32]
    library.seccomp_init.restype = ctypes.c_void_p
    library.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    library.seccomp_syscall_resolve_name.restype = ctypes.c_int
    library.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
    library.seccomp_rule_add.restype = ctypes.c_int
    library.seccomp_load.argtypes = [ctypes.c_void_p]
    library.seccomp_load.restype = ctypes.c_int
    library.seccomp_release.argtypes = [ctypes.c_void_p]
    context = library.seccomp_init(0x7fff0000)  # SCMP_ACT_ALLOW; unknown architectures are killed by libseccomp.
    if not context:
        raise IsolationUnavailable('cannot allocate syscall filter')
    denied = ('socket', 'socketpair', 'connect', 'bind', 'listen', 'accept', 'accept4',
              'execve', 'execveat', 'fork', 'vfork', 'clone', 'clone3', 'ptrace',
              'process_vm_readv', 'process_vm_writev', 'kill', 'tkill', 'tgkill',
              'pidfd_open', 'pidfd_getfd', 'pidfd_send_signal', 'mount', 'umount2',
              'pivot_root', 'chroot', 'setns', 'unshare', 'bpf', 'perf_event_open',
              'userfaultfd', 'io_uring_setup', 'io_uring_enter', 'io_uring_register',
              'open_by_handle_at', 'init_module', 'finit_module', 'delete_module',
              'setrlimit', 'prlimit64', 'prctl', 'seccomp')
    try:
        for name in denied:
            number = library.seccomp_syscall_resolve_name(name.encode('ascii'))
            if number >= 0 and library.seccomp_rule_add(context, 0x00050000 | errno.EPERM, number, 0) != 0:
                raise IsolationUnavailable('cannot construct syscall denial rules')
        if library.seccomp_load(context) != 0:
            raise IsolationUnavailable('kernel refused parser syscall isolation')
    finally:
        library.seccomp_release(context)

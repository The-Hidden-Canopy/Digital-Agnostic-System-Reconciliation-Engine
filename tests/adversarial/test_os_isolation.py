"""Probe the real OS boundary, including native syscalls that bypass Python guards.

Run on the declared Linux host with bubblewrap/libseccomp; a missing boundary is a failure,
not a skipped security gate. Portable mode has a separate explicit behavior test.
"""
import errno
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from verifier_lab.ingestion.common import Limits
from verifier_lab.ingestion.extract import ExtractionFailure, extract_isolated, PKG_ROOT
from verifier_lab.ingestion.isolation import command


class ParserIsolation(unittest.TestCase):
    def test_host_files_native_sockets_and_child_processes_are_blocked(self):
        with tempfile.TemporaryDirectory(prefix='vl-host-private-') as directory:
            sentinel = Path(directory) / 'private.txt'
            sentinel.write_text('must not be visible in parser namespace')
            code = '''import sys,json,os,ctypes,errno,math
sys.path.insert(0,sys.argv[1])
from verifier_lab.ingestion.worker import _apply_sandbox
from verifier_lab.ingestion.common import Limits
_apply_sandbox(Limits())
checks={}
try:
 open(sys.argv[2]).read()
 checks['host_file_hidden']=False
except OSError:
 checks['host_file_hidden']=True
libc=ctypes.CDLL(None,use_errno=True)
checks['native_socket_denied']=libc.socket(2,1,0)==-1 and ctypes.get_errno()==errno.EPERM
try:
 pid=os.fork()
 if pid==0: os._exit(0)
 os.waitpid(pid,0)
 checks['fork_denied']=False
except OSError as exc:
 checks['fork_denied']=exc.errno==errno.EPERM
try:
 os.execv(sys.executable,[sys.executable,'-c','raise SystemExit(99)'])
except OSError as exc:
 checks['exec_denied']=exc.errno==errno.EPERM
checks['namespace_private']=os.getppid()<=2
checks['runtime_readable']=math.sqrt(4)==2
print(json.dumps(checks))
'''
            argv = command([sys.executable, '-E', '-s', '-B', '-c', code, PKG_ROOT, str(sentinel)], PKG_ROOT, 'required')
            result = subprocess.run(argv, capture_output=True, text=True, env={'LANG': 'C.UTF-8'}, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            checks = json.loads(result.stdout)
            self.assertEqual(len(checks), 6)
            self.assertTrue(all(checks.values()), checks)

    def test_missing_boundary_fails_closed_and_portable_is_explicit(self):
        with patch('verifier_lab.ingestion.isolation.shutil.which', return_value=None):
            with self.assertRaises(ExtractionFailure) as ctx:
                extract_isolated('json', b'{"x":1}', None, Limits())
            self.assertEqual(ctx.exception.code, 'isolation_unavailable')
            result = extract_isolated('json', b'{"x":1}', None, Limits(isolation='portable'))
            self.assertEqual(result['facts'][0]['key'], 'x')

    def test_unsupported_modes_cannot_disable_boundary(self):
        with self.assertRaises(ExtractionFailure):
            extract_isolated('json', b'{}', None, Limits(isolation='off'))

    def test_worker_output_is_bounded_before_parent_accumulates_it(self):
        for channel, count in (('stdout', 10000), ('stderr', 100000)):
            argv = [sys.executable, '-c', f'import sys; sys.{channel}.write("x"*{count})']
            with self.subTest(channel=channel), \
                    patch('verifier_lab.ingestion.extract.command', return_value=argv), \
                    patch('verifier_lab.ingestion.extract.MAX_OUTPUT', 32):
                with self.assertRaises(ExtractionFailure) as ctx:
                    extract_isolated('json', b'{}', None, Limits(isolation='portable'))
                self.assertEqual(ctx.exception.code, 'output_limit')

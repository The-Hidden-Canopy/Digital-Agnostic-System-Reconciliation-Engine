"""A small pool of long-lived parser processes for trusted repository scans.

Each worker reads source bytes on a pipe and answers with a syntax record. Repository code is
never imported or run. This pool applies no operating-system sandbox, so it is used only when
the caller has explicitly chosen trusted (portable) parsing; required isolation keeps the
original one-sandboxed-process-per-file path.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading

from .ingestion.common import Limits
from .ingestion.extract import BOOT, PKG_ROOT

MAX_REPLY_BYTES = 64_000_000


class WorkerFailure(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


class ParserWorker:
    """One parser process. Not thread-safe: each scanning thread owns one worker."""

    def __init__(self, limits: Limits):
        if limits.isolation != "portable":
            raise ValueError("the parser pool is available only for trusted parsing")
        self.limits = limits
        self.process = None
        self.directory = None
        self.restarts = 0

    def _start(self):
        request = json.dumps({"batch": True, "limits": self.limits.to_dict()})
        self.directory = tempfile.TemporaryDirectory(prefix="vl-pool-")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        self.process = subprocess.Popen(
            [sys.executable, "-E", "-s", "-B", "-c", BOOT, PKG_ROOT, request],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, cwd=self.directory.name,
            env={"PATH": "", "LANG": "C.UTF-8", "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")}, creationflags=flags)

    def _read(self, size):
        chunks, remaining = [], size
        while remaining:
            chunk = self.process.stdout.read(remaining)
            if not chunk:
                raise WorkerFailure("worker_stopped")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def parse(self, fmt: str, data: bytes, dialect: str) -> dict:
        """Return the parsed record, or raise WorkerFailure with a short reason code."""
        if self.process is None or self.process.poll() is not None:
            self.close()
            self._start()
        timed_out = threading.Event()

        def expire():
            timed_out.set()
            try:
                self.process.kill()
            except OSError:
                pass
        timer = threading.Timer(self.limits.wall_seconds, expire)
        timer.daemon = True
        timer.start()
        try:
            header = json.dumps({"format": fmt, "dialect": dialect, "size": len(data)}).encode("utf-8")
            self.process.stdin.write(len(header).to_bytes(4, "big") + header + data)
            self.process.stdin.flush()
            size = int.from_bytes(self._read(4), "big")
            if size > MAX_REPLY_BYTES:
                raise WorkerFailure("output_limit")
            reply = json.loads(self._read(size))
        except (OSError, ValueError, WorkerFailure) as exc:
            self.restarts += 1
            self.close()
            if timed_out.is_set():
                raise WorkerFailure("wall_clock_timeout") from exc
            raise WorkerFailure(exc.code if isinstance(exc, WorkerFailure) else "worker_stopped") from exc
        finally:
            timer.cancel()
        if not reply.get("ok"):
            raise WorkerFailure(str(reply.get("error") or "worker_error")[:80])
        return reply["extraction"]

    def close(self):
        process, self.process = self.process, None
        if process is not None:
            for stream in (process.stdin, process.stdout):
                try:
                    stream.close()
                except OSError:
                    pass
            if process.poll() is None:
                try:
                    process.kill()
                except OSError:
                    pass
            try:
                process.wait(timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                pass
        directory, self.directory = self.directory, None
        if directory is not None:
            try:
                directory.cleanup()
            except OSError:
                pass


def default_worker_count():
    return max(1, min(6, (os.cpu_count() or 2) - 1))

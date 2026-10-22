"""Adapter for a user-supplied subject program (chapter 05: candidate subjects use their own implementation).

Protocol: JSON lines. The adapter writes one request line to the program's stdin and reads one response line from its stdout.

  {"cmd":"init","seed":S}                    -> {"ok":true}
  {"cmd":"apply","op":{...}}                 -> {"ok":true,"accepted":bool,"observations":[str,...]}
  {"cmd":"snapshot"}                         -> {"ok":true,"state":{entries,next_identifier,logical_tick,random_cursor,operation_cursor}}
  {"cmd":"load","state":{...}}               -> {"ok":true}
  {"cmd":"replay","ops":[...]}  (optional)   -> {"ok":true,"results":[{"accepted":bool,"observations":[...]},...]}
                                                or {"ok":false,"error":"unsupported"} to fall back to sequential apply

Every "instance" is a fresh process (continuous, resumed and replayed runs never share memory). The program is ordinary
software the *user* chose on the command line: it is run with an empty working directory, a minimal environment and
resource limits, but this is containment against accidents, NOT a security boundary for hostile code. A command is never
read from an imported file, an uploaded package or the web API.
"""
from __future__ import annotations

import hashlib
import json
import os
import select
import subprocess
import tempfile
import time
from pathlib import Path

MAX_LINE = 4_000_000


class SubjectError(Exception):
    """Operational failure of the subject (crash, timeout, protocol violation). Never a verdict about the subject's logic."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code


def subject_identity(command: list[str], env: dict | None = None) -> str:
    """Pins *which program* produced a result: the command plus the bytes of any file argument that exists."""
    h = hashlib.sha256()
    h.update(json.dumps([command, sorted((env or {}).items())]).encode())
    for part in command:
        p = Path(part)
        if p.is_file():
            h.update(b"\x00" + hashlib.sha256(p.read_bytes()).digest())
    return "sha256:" + h.hexdigest()


def _limits():
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_CPU, (60, 62))
        resource.setrlimit(resource.RLIMIT_AS, (2_000_000_000, 2_000_000_000))
        resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
    except Exception:
        pass


class ExternalSubject:
    """Speaks the protocol to one fresh process. Mirrors the CandidateStore interface used by the experiments."""

    def __init__(self, command: list[str], seed: str, timeout: float = 10.0, env: dict | None = None):
        if not command or not all(isinstance(c, str) for c in command):
            raise SubjectError("bad_command")
        self.command, self.timeout = command, timeout
        self._cwd = tempfile.TemporaryDirectory(prefix="vl-subject-")
        try:
            self.proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                         cwd=self._cwd.name, env={"PATH": os.environ.get("PATH", ""), "LANG": "C.UTF-8", **(env or {})},
                                         preexec_fn=_limits, close_fds=True)
        except OSError as exc:
            self._cwd.cleanup()
            raise SubjectError("cannot_start", str(exc)) from exc
        self._buf = b""
        try:
            self._call({"cmd": "init", "seed": seed})
        except SubjectError:
            self.close()
            raise

    def _call(self, req: dict) -> dict:
        if self.proc.poll() is not None:
            raise SubjectError("subject_exited", f"exit code {self.proc.returncode}")
        try:
            self.proc.stdin.write(json.dumps(req).encode() + b"\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise SubjectError("subject_exited", "pipe closed") from exc
        deadline = time.monotonic() + self.timeout
        while b"\n" not in self._buf:
            left = deadline - time.monotonic()
            if left <= 0:
                raise SubjectError("timeout", f"no response within {self.timeout}s")
            ready, _, _ = select.select([self.proc.stdout], [], [], min(left, 0.2))
            if ready:
                chunk = os.read(self.proc.stdout.fileno(), 65536)
                if not chunk:
                    raise SubjectError("subject_exited", "stdout closed")
                self._buf += chunk
                if len(self._buf) > MAX_LINE:
                    raise SubjectError("response_too_large")
            elif self.proc.poll() is not None:
                raise SubjectError("subject_exited", f"exit code {self.proc.returncode}")
        line, _, self._buf = self._buf.partition(b"\n")
        try:
            resp = json.loads(line)
        except ValueError as exc:
            raise SubjectError("protocol_violation", "response is not JSON") from exc
        if not isinstance(resp, dict) or "ok" not in resp:
            raise SubjectError("protocol_violation", "response needs an 'ok' field")
        return resp

    def _ok(self, req: dict) -> dict:
        resp = self._call(req)
        if resp["ok"] is not True:
            raise SubjectError("subject_error", str(resp.get("error", ""))[:200])
        return resp

    # ---- CandidateStore-compatible surface
    def apply(self, op: dict):
        r = self._ok({"cmd": "apply", "op": op})
        if not isinstance(r.get("accepted"), bool) or not isinstance(r.get("observations", []), list) \
                or not all(isinstance(x, str) for x in r.get("observations", [])):
            raise SubjectError("protocol_violation", "apply needs accepted:bool and observations:[str]")
        return r["accepted"], tuple(r.get("observations", ()))

    def snapshot(self) -> dict:
        r = self._ok({"cmd": "snapshot"})
        state = r.get("state")
        if not isinstance(state, dict):
            raise SubjectError("protocol_violation", "snapshot needs a state object")
        return state

    def load(self, state: dict) -> None:
        self._ok({"cmd": "load", "state": state})

    def replay(self, ops: list[dict]):
        resp = self._call({"cmd": "replay", "ops": ops})
        if resp.get("ok") is True and isinstance(resp.get("results"), list) and len(resp["results"]) == len(ops):
            for op, r in zip(ops, resp["results"]):
                yield (op, bool(r["accepted"]), tuple(r.get("observations", ())))
            return
        if resp.get("ok") is False and resp.get("error") == "unsupported":
            for op in ops:                       # the program has no replay entry point: a sequential replay is what it can do
                yield (op, *self.apply(op))
            return
        raise SubjectError("protocol_violation", "replay needs one result per operation or error 'unsupported'")

    def close(self) -> None:
        try:
            if self.proc.poll() is None:
                self.proc.kill()
            self.proc.wait(timeout=5)
        except Exception:
            pass
        for f in (self.proc.stdin, self.proc.stdout):
            try:
                f.close()
            except Exception:
                pass
        self._cwd.cleanup()


class ExternalFactory:
    """Callable creating a fresh subject process per instance; remembers every instance to close it."""

    def __init__(self, command: list[str], timeout: float = 10.0, env: dict | None = None):
        # the subject runs in an empty working directory, so file arguments must be absolute
        command = [str(Path(c).resolve()) if Path(c).is_file() else c for c in command]
        self.command, self.timeout, self.env = command, timeout, dict(env or {})
        self.identity = subject_identity(command, self.env)
        self._live: list[ExternalSubject] = []

    def __call__(self, seed: str, defects=frozenset()):
        if defects:
            raise SubjectError("defects_not_supported", "defect injection applies to the built-in candidate only")
        s = ExternalSubject(self.command, seed, self.timeout, self.env)
        self._live.append(s)
        return s

    def close_all(self) -> None:
        for s in self._live:
            s.close()
        self._live.clear()

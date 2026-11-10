"""Experimental file transport for synthetic trusted tests; this is not isolation.

Unlike the production pipe transport, source bytes touch a temporary directory.
Do not use this prototype for private repositories: an abnormal termination can
leave temporary files, and directory ACLs and hostile-child behavior are not yet
validated. It is deliberately unavailable for required-isolation mode.
"""
from __future__ import annotations

import json
import math
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

from .common import Limits
from .extract import ExtractionFailure, MAX_OUTPUT, PKG_ROOT

FORMATS = frozenset({"repository-python", "repository-javascript", "repository-typescript", "repository-tsx", "repository-jsx", "repository-cpp"})
WORKER_ERRORS = frozenset({"input_limit", "output_limit", "isolation_unavailable", "memory_limit", "recursion_limit"})
BOOT = ("import sys; sys.path.insert(0, sys.argv[1]); "
        "from verifier_lab.ingestion.file_exchange import worker_main; sys.exit(worker_main(sys.argv[1:]))")


def _run_file_worker(request, input_file, output_file, limits, cwd):
    argv = [sys.executable, "-E", "-s", "-B", "-c", BOOT, PKG_ROOT, request, str(input_file), str(output_file)]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    try:
        proc = subprocess.Popen(argv, cwd=cwd, close_fds=True, creationflags=flags, env={"PATH": "", "LANG": "C.UTF-8"})
        try:
            proc.wait(timeout=limits.wall_seconds)
        except subprocess.TimeoutExpired as exc:
            proc.kill(); proc.wait()
            raise ExtractionFailure("wall_clock_timeout") from exc
    except OSError as exc:
        raise ExtractionFailure("worker_unavailable") from exc
    if proc.returncode != 0:
        raise ExtractionFailure("worker_crashed", f"exit {proc.returncode}")


def _decode_reply(payload: bytes) -> dict:
    """Reject ambiguous envelopes from an untrusted worker; not a graph schema validator."""
    def unique_object(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate JSON key")
            value[key] = item
        return value

    def invalid_constant(_value):
        raise ValueError("non-finite JSON number")

    def finite_float(value):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("non-finite JSON number")
        return number

    try:
        result = json.loads(payload.decode("utf-8"), object_pairs_hook=unique_object, parse_constant=invalid_constant, parse_float=finite_float)
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise ExtractionFailure("worker_protocol_error") from exc
    if not isinstance(result, dict) or type(result.get("ok")) is not bool:
        raise ExtractionFailure("worker_protocol_error")
    if result["ok"]:
        if set(result) != {"ok", "extraction"} or not isinstance(result["extraction"], dict):
            raise ExtractionFailure("worker_protocol_error")
        return result["extraction"]
    if set(result) != {"ok", "error"} or not isinstance(result["error"], str) or result["error"] not in WORKER_ERRORS:
        raise ExtractionFailure("worker_protocol_error")
    raise ExtractionFailure(result["error"])


def experimental_file_exchange(fmt: str, data: bytes, dialect: str | None, limits: Limits) -> dict:
    """Exercise a no-inherited-pipe protocol in explicitly portable test mode only."""
    if limits.isolation != "portable":
        raise ExtractionFailure("isolation_unavailable", "The file transport is not an isolation boundary")
    if fmt not in FORMATS or not isinstance(data, bytes) or len(data) > limits.max_bytes:
        raise ExtractionFailure("bad_exchange_request")
    request = json.dumps({"format": fmt, "dialect": dialect, "limits": limits.to_dict()}, ensure_ascii=True)
    temporary_root = Path(tempfile.gettempdir()).resolve()
    with tempfile.TemporaryDirectory(prefix="elucidary-exchange-", dir=temporary_root) as scratch:
        root = Path(scratch).resolve()
        if root.parent != temporary_root:
            raise ExtractionFailure("worker_protocol_error")
        input_file, output_file = root / "input.bin", root / "result.json"
        with input_file.open("xb") as handle:
            handle.write(data)
        _run_file_worker(request, input_file, output_file, limits, scratch)
        try:
            if os.name == "nt":
                from .windows_reply_guard import ReplyGuardError, read_reply
                try:
                    payload = read_reply(root, MAX_OUTPUT)
                except ReplyGuardError as exc:
                    raise ExtractionFailure(exc.code) from exc
            else:
                if not stat.S_ISREG(output_file.lstat().st_mode) or output_file.is_symlink():
                    raise ExtractionFailure("worker_protocol_error")
                with output_file.open("rb") as handle:
                    payload = handle.read(MAX_OUTPUT + 1)
        except OSError as exc:
            raise ExtractionFailure("worker_protocol_error") from exc
        if len(payload) > MAX_OUTPUT:
            raise ExtractionFailure("output_limit")
        from .graph_reply_schema import validate_graph_result
        path = dialect or ("source.py" if fmt == "repository-python" else "source.js" if fmt == "repository-javascript" else "source")
        return validate_graph_result(_decode_reply(payload), fmt, data, path)


def worker_main(argv: list[str]) -> int:
    """File-side worker used only by the portable protocol experiment."""
    from .worker import dispatch
    request = json.loads(argv[1]); limits = Limits(**request["limits"])
    if limits.isolation != "portable" or request.get("format") not in FORMATS:
        return 2
    with Path(argv[2]).open("rb") as handle:
        data = handle.read(limits.max_bytes + 1)
    result = {"ok": False, "error": "input_limit"} if len(data) > limits.max_bytes else dispatch(request, data)
    payload = json.dumps(result, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    if len(payload) > MAX_OUTPUT:
        payload = b'{"ok":false,"error":"output_limit"}'
    with Path(argv[3]).open("xb") as handle:
        handle.write(payload)
    return 0

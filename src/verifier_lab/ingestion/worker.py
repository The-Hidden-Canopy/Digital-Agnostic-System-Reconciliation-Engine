"""Bounded extraction worker. Runs in a short-lived subprocess: no network, no shell, no workspace."""
from __future__ import annotations

import base64
import json
import os
import sys

from .common import Extraction, Limits


def run_in_process(fmt: str, data: bytes, dialect: str | None, limits: Limits) -> Extraction:
    if fmt == "json":
        from .adapters import json_adapter
        return json_adapter.extract(data, limits)
    if fmt == "markdown":
        from .adapters import markdown_adapter
        return markdown_adapter.extract(data, limits)
    if fmt == "sql":
        from .adapters import sql_adapter
        return sql_adapter.extract(data, limits, dialect)
    if fmt == "pdf":
        from .adapters import pdf_adapter
        return pdf_adapter.extract(data, limits)
    raise ValueError(f"unsupported format {fmt}")


def to_wire(ext: Extraction) -> dict:
    return {"format": ext.format, "parser": ext.parser, "parser_version": ext.parser_version,
            "rendition_b64": None if ext.rendition is None else base64.b64encode(ext.rendition).decode("ascii"),
            "rendition_media_type": ext.rendition_media_type, "facts": ext.facts,
            "regions": ext.regions, "diagnostics": ext.diagnostics,
            "blocked_keys": ext.blocked_keys, "dialect": ext.dialect}


def _apply_sandbox(limits: Limits) -> None:
    try:
        import resource
        # Linux permits lowering RLIMIT_AS below the already loaded interpreter. Diagnose that
        # as an exhausted memory budget before a later loader error masquerades as missing isolation.
        if sys.platform == "linux":
            with open("/proc/self/statm", encoding="ascii") as usage:
                current_bytes = int(usage.read().split()[0]) * os.sysconf("SC_PAGE_SIZE")
            if current_bytes > limits.memory_bytes:
                raise MemoryError("the parser memory budget is smaller than its trusted runtime")
        resource.setrlimit(resource.RLIMIT_CPU, (limits.cpu_seconds, limits.cpu_seconds + 2))
        resource.setrlimit(resource.RLIMIT_AS, (limits.memory_bytes, limits.memory_bytes))
        resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
    except (ImportError, ValueError, OSError):
        if limits.isolation == "required":
            from .isolation import IsolationUnavailable
            raise IsolationUnavailable("required parser resource limits are unavailable")
    if limits.isolation == "required":
        from .isolation import deny_syscalls
        deny_syscalls()
    # Defense in depth: adapters contain no network code; make accidental use fail loudly.
    import socket

    def _blocked(*a, **k):
        raise OSError("network access is disabled in the extraction worker")
    socket.socket = _blocked  # type: ignore[assignment]
    socket.create_connection = _blocked  # type: ignore[assignment]
    socket.getaddrinfo = _blocked  # type: ignore[assignment]


REPOSITORY_TREE_FORMATS = ("repository-typescript", "repository-tsx", "repository-jsx", "repository-cpp",
                           "repository-c", "repository-csharp", "repository-rust")


def parse_repository(fmt: str, data: bytes, dialect: str | None) -> dict:
    """Read one repository source file as syntax. Never imports or runs it."""
    if fmt == "repository-python":
        from ..repository_map import parse_python
        return parse_python(data, dialect or "source.py")
    if fmt in REPOSITORY_TREE_FORMATS:
        from ..repository_trees import parse_source
        return parse_source(data, dialect or "source", fmt.removeprefix("repository-"))
    if fmt == "repository-javascript":
        # The tree-sitter grammar reads current syntax, class methods and CommonJS; the older
        # pure-Python parser remains for installations that have only the javascript extra.
        from ..repository_trees import parse_source
        result = parse_source(data, dialect or "source.js", "javascript")
        if str(result.get("error") or "").startswith("language parser unavailable"):
            from ..repository_javascript import parse_javascript
            return parse_javascript(data, dialect or "source.js")
        return result
    raise ValueError(f"unsupported format {fmt}")


def dispatch(request: dict, data: bytes, *, sandboxed: bool = False) -> dict:
    from .isolation import IsolationUnavailable
    limits = Limits(**request["limits"])
    try:
        if not sandboxed:
            _apply_sandbox(limits)
        if request["format"].startswith("repository-"):
            out = {"ok": True, "extraction": parse_repository(request["format"], data, request.get("dialect"))}
        else:
            ext = run_in_process(request["format"], data, request.get("dialect"), limits)
            out = {"ok": True, "extraction": to_wire(ext)}
    except IsolationUnavailable:
        out = {"ok": False, "error": "isolation_unavailable"}
    except MemoryError:
        out = {"ok": False, "error": "memory_limit"}
    except RecursionError:
        out = {"ok": False, "error": "recursion_limit"}
    return out


def _read_exact(stream, size: int) -> bytes | None:
    chunks, remaining = [], size
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            return None
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def serve_batch(request: dict) -> int:
    """Serve many repository files over one pipe pair: framed request in, framed JSON out.

    Used only for trusted (portable) repository scans, where starting one process per file
    dominates the scan time. The parent bounds each file's wall time and replaces a worker
    that stops answering, so one bad file costs one result rather than the scan.
    """
    limits = Limits(**request["limits"])
    stdin, stdout = sys.stdin.buffer, sys.stdout.buffer
    try:
        _apply_sandbox(limits)
    except Exception:
        return 3
    sys.setrecursionlimit(max(sys.getrecursionlimit(), 6000))
    while True:
        head = _read_exact(stdin, 4)
        if head is None:
            return 0
        header = _read_exact(stdin, int.from_bytes(head, "big"))
        if header is None:
            return 0
        item = json.loads(header)
        data = _read_exact(stdin, int(item["size"]))
        if data is None:
            return 0
        try:
            out = dispatch({"format": item["format"], "dialect": item.get("dialect"), "limits": request["limits"]},
                           data, sandboxed=True)
        except Exception as exc:  # report and keep serving; the parent records the file as unreadable syntax
            out = {"ok": False, "error": "parser_error:" + type(exc).__name__}
        payload = json.dumps(out, separators=(",", ":")).encode("utf-8")
        stdout.write(len(payload).to_bytes(4, "big") + payload)
        stdout.flush()


def main(argv: list[str]) -> int:
    request = json.loads(argv[1])
    if request.get("batch"):
        return serve_batch(request)
    data = sys.stdin.buffer.read()
    out = dispatch(request, data)
    sys.stdout.write(json.dumps(out))
    return 0

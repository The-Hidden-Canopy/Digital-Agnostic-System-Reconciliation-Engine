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


def main(argv: list[str]) -> int:
    from .isolation import IsolationUnavailable
    request = json.loads(argv[1])
    limits = Limits(**request["limits"])
    data = sys.stdin.buffer.read()
    try:
        _apply_sandbox(limits)
        ext = run_in_process(request["format"], data, request.get("dialect"), limits)
        out = {"ok": True, "extraction": to_wire(ext)}
    except IsolationUnavailable:
        out = {"ok": False, "error": "isolation_unavailable"}
    except MemoryError:
        out = {"ok": False, "error": "memory_limit"}
    except RecursionError:
        out = {"ok": False, "error": "recursion_limit"}
    sys.stdout.write(json.dumps(out))
    return 0

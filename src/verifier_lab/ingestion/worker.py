"""Bounded extraction worker. Runs in a short-lived subprocess: no network, no shell, no workspace."""
from __future__ import annotations

import base64
import json
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
        resource.setrlimit(resource.RLIMIT_CPU, (limits.cpu_seconds, limits.cpu_seconds + 2))
        resource.setrlimit(resource.RLIMIT_AS, (limits.memory_bytes, limits.memory_bytes))
        resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
    except (ImportError, ValueError, OSError):
        pass  # non-POSIX: documented limitation, wall-clock timeout still applies
    # Defense in depth: adapters contain no network code; make accidental use fail loudly.
    import socket

    def _blocked(*a, **k):
        raise OSError("network access is disabled in the extraction worker")
    socket.socket = _blocked  # type: ignore[assignment]
    socket.create_connection = _blocked  # type: ignore[assignment]
    socket.getaddrinfo = _blocked  # type: ignore[assignment]


def main(argv: list[str]) -> int:
    request = json.loads(argv[1])
    limits = Limits(**request["limits"])
    data = sys.stdin.buffer.read()
    _apply_sandbox(limits)
    try:
        ext = run_in_process(request["format"], data, request.get("dialect"), limits)
        out = {"ok": True, "extraction": to_wire(ext)}
    except MemoryError:
        out = {"ok": False, "error": "memory_limit"}
    except RecursionError:
        out = {"ok": False, "error": "recursion_limit"}
    sys.stdout.write(json.dumps(out))
    return 0

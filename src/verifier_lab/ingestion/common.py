"""Shared extraction contracts: facts, spans, coverage regions (chapter 17)."""
from __future__ import annotations

from dataclasses import dataclass, field

from ..canonical import record_id, sha256_digest

PARSED, PARTIAL, UNSUPPORTED, CORRUPT = "PARSED", "PARTIALLY_PARSED", "UNSUPPORTED", "CORRUPT"
ENCRYPTED, LIMIT, NOT_ATTEMPTED = "ENCRYPTED", "RESOURCE_LIMIT", "NOT_ATTEMPTED"
COVERAGE_STATES = (PARSED, PARTIAL, UNSUPPORTED, CORRUPT, ENCRYPTED, LIMIT, NOT_ATTEMPTED)

ADAPTER_VERSION = "1"


@dataclass
class Limits:
    max_bytes: int = 5_000_000
    max_depth: int = 32
    max_tokens: int = 400_000
    max_facts: int = 2_000
    max_pages: int = 200
    max_statements: int = 5_000
    max_text_chars: int = 2_000_000
    cpu_seconds: int = 30
    memory_bytes: int = 1_500_000_000
    wall_seconds: int = 60

    def to_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class Extraction:
    format: str
    parser: str
    parser_version: str
    rendition: bytes | None = None          # None => the source bytes are the rendition
    rendition_media_type: str = "text/plain"
    facts: list[dict] = field(default_factory=list)
    regions: list[dict] = field(default_factory=list)
    diagnostics: list[dict] = field(default_factory=list)
    blocked_keys: list[dict] = field(default_factory=list)
    dialect: str | None = None

    def diag(self, code: str, **detail) -> None:
        self.diagnostics.append({"code": code, **detail})


class ByteMap:
    """char index -> byte offset in UTF-8, for locators that must index raw bytes."""

    def __init__(self, text: str):
        self.ascii = text.isascii()
        self.text = text
        if not self.ascii:
            offs = [0]
            total = 0
            for ch in text:
                total += len(ch.encode("utf-8"))
                offs.append(total)
            self.offs = offs

    def __call__(self, i: int) -> int:
        return i if self.ascii else self.offs[i]


def region(kind: str, start: int, end: int, status: str, interpretation: str,
           reasons: tuple[str, ...] = (), detail: str = "") -> dict:
    assert status in COVERAGE_STATES
    return {"kind": kind, "byte_start": start, "byte_end": end, "status": status,
            "interpretation": interpretation, "reason_codes": list(reasons), "detail": detail}


def make_fact(key: str, value: dict, grammar: str, rendition: bytes, start: int, end: int,
              locator_extra: dict, method: str, context: tuple[int, int] | None = None,
              qualifiers: tuple[str, ...] = ()) -> dict:
    token = rendition[start:end]
    loc = {"byte_start": start, "byte_end": end, **locator_extra}
    if context:
        loc["context_start"], loc["context_end"] = context
    return {"key": key, "value": value, "grammar": grammar, "locator": loc,
            "token_digest": sha256_digest(token), "extraction_method": method,
            "qualifiers": list(qualifiers)}


WS_STRUCT = set(" \t\r\n{}[],:\x0c")


def finalize_regions(regions: list[dict], data: bytes, structural=WS_STRUCT) -> list[dict]:
    """Fill gaps that are pure whitespace/punctuation; leave unknown extent as NOT_ATTEMPTED."""
    out: list[dict] = []
    pos = 0
    for r in sorted(regions, key=lambda r: (r["byte_start"], r["byte_end"])):
        if r["byte_start"] > pos:
            out.append(_gap(data, pos, r["byte_start"], structural))
        out.append(r)
        pos = max(pos, r["byte_end"])
    if pos < len(data):
        out.append(_gap(data, pos, len(data), structural))
    return out


def _gap(data: bytes, a: int, b: int, structural) -> dict:
    chunk = data[a:b].decode("utf-8", errors="replace")
    if all(c in structural for c in chunk):
        return region("structure", a, b, PARSED, "NONCLAIM", ("whitespace_or_punctuation",))
    return region("unaccounted", a, b, NOT_ATTEMPTED, "NONE", ("unaccounted_bytes",))


def summarize(regions: list[dict]) -> str:
    """File-level coverage is derived from its regions, never hand-assigned."""
    if not regions:
        return NOT_ATTEMPTED
    states = {r["status"] for r in regions}
    if states == {PARSED}:
        return PARSED
    productive = states & {PARSED, PARTIAL}
    if not productive and len(states) == 1:
        return next(iter(states))
    if not productive:
        for s in (CORRUPT, ENCRYPTED, LIMIT, UNSUPPORTED, NOT_ATTEMPTED):
            if s in states:
                return s
    return PARTIAL


def extraction_manifest(source_digest: str, ext: Extraction, limits: Limits, options: dict) -> dict:
    body = {"source_digest": source_digest, "format": ext.format, "parser": ext.parser,
            "parser_version": ext.parser_version, "adapter_version": ADAPTER_VERSION,
            "declared_dialect": ext.dialect, "options": options, "limits": limits.to_dict(),
            "locale": "none", "ocr": None}
    return {"id": record_id("extraction", body), **body}

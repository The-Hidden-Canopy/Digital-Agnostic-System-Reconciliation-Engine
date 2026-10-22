"""Canonical JSON encoding, strict parsing and domain-separated identities (chapters 01-02)."""
from __future__ import annotations

import hashlib
import json
from typing import Any

JsonValue = Any  # str | int | bool | None | list | dict[str, JsonValue]

MAX_DEPTH = 64


class CanonicalError(ValueError):
    """Raised for values outside the restricted JSON model."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code


def validate_value(value: JsonValue, _depth: int = 0) -> None:
    if _depth > MAX_DEPTH:
        raise CanonicalError("depth_exceeded")
    if value is None or isinstance(value, (bool, str)):
        if isinstance(value, str):
            try:
                value.encode("utf-8", errors="strict")
            except UnicodeEncodeError as exc:
                raise CanonicalError("invalid_unicode", str(exc)) from exc
        return
    if isinstance(value, int):
        return
    if isinstance(value, float):
        raise CanonicalError("float_forbidden")
    if isinstance(value, (list, tuple)):
        for item in value:
            validate_value(item, _depth + 1)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise CanonicalError("non_string_key")
            validate_value(key, _depth + 1)
            validate_value(item, _depth + 1)
        return
    raise CanonicalError("unsupported_type", type(value).__name__)


def canonical_bytes(value: JsonValue) -> bytes:
    validate_value(value)
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8", errors="strict")


def sha256_digest(data: bytes) -> str:
    """Artifact digest: SHA-256 over raw bytes, algorithm-tagged."""
    return "sha256:" + hashlib.sha256(data).hexdigest()


def record_id(kind: str, body_without_id: JsonValue) -> str:
    """Type-prefixed, domain-separated identity of a canonical record body."""
    payload = kind.encode("ascii") + b"\x00" + canonical_bytes(body_without_id)
    return kind + ":sha256:" + hashlib.sha256(payload).hexdigest()


def parse_digest(text: str) -> bool:
    if not isinstance(text, str) or not text.startswith("sha256:"):
        return False
    hexpart = text[7:]
    return len(hexpart) == 64 and all(c in "0123456789abcdef" for c in hexpart)


def parse_record_id(kind: str, text: str) -> bool:
    prefix = kind + ":sha256:"
    if not isinstance(text, str) or not text.startswith(prefix):
        return False
    hexpart = text[len(prefix):]
    return len(hexpart) == 64 and all(c in "0123456789abcdef" for c in hexpart)


def _scan_depth(data: bytes, limit: int) -> None:
    """Bounded pre-scan so hostile nesting never reaches the recursive parser."""
    depth = 0
    in_string = False
    escaped = False
    for byte in data:
        if in_string:
            if escaped:
                escaped = False
            elif byte == 0x5C:
                escaped = True
            elif byte == 0x22:
                in_string = False
            continue
        if byte == 0x22:
            in_string = True
        elif byte in (0x5B, 0x7B):
            depth += 1
            if depth > limit:
                raise CanonicalError("depth_exceeded")
        elif byte in (0x5D, 0x7D):
            depth -= 1


def _reject_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise CanonicalError("duplicate_key", key)
        out[key] = value
    return out


def _reject_constant(name: str) -> Any:
    raise CanonicalError("nonfinite_constant", name)


def _reject_float(text: str) -> Any:
    raise CanonicalError("float_forbidden", text)


def strict_loads(data: bytes, *, max_bytes: int = 4_000_000, max_depth: int = MAX_DEPTH) -> JsonValue:
    """Parse normative JSON: strict UTF-8, no duplicate keys, floats or non-finite constants."""
    if len(data) > max_bytes:
        raise CanonicalError("size_limit", f"{len(data)} > {max_bytes}")
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise CanonicalError("invalid_utf8", str(exc)) from exc
    if text.startswith("﻿"):
        raise CanonicalError("bom_forbidden")
    _scan_depth(data, max_depth)
    try:
        value = json.loads(
            text,
            object_pairs_hook=_reject_pairs,
            parse_constant=_reject_constant,
            parse_float=_reject_float,
        )
    except CanonicalError:
        raise
    except RecursionError as exc:
        raise CanonicalError("depth_exceeded") from exc
    except json.JSONDecodeError as exc:
        raise CanonicalError("malformed_json", str(exc)) from exc
    validate_value(value)
    return value

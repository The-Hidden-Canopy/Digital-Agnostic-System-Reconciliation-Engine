"""Byte admission: bounded, content-addressed, type-checked. Filenames are inert metadata."""
from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ..canonical import sha256_digest

EXT = {".pdf": "pdf", ".json": "json", ".md": "markdown", ".markdown": "markdown", ".sql": "sql"}
TYPE_NAMES = ("pdf", "json", "markdown", "sql")
ARCHIVE_MAGIC = (b"PK\x03\x04", b"PK\x05\x06", b"\x1f\x8b", b"7z\xbc\xaf", b"Rar!", b"BZh",
                 b"\xfd7zXZ\x00", b"\x28\xb5\x2f\xfd")


class AdmissionError(Exception):
    def __init__(self, code: str, message: str = ""):
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class AdmissionLimits:
    max_bytes: int = 8_000_000


@dataclass(frozen=True)
class Admitted:
    digest: str
    size: int
    detected_format: str
    display_name: str


def safe_display_name(name: str | None) -> str:
    name = (name or "unnamed").replace("\\", "/").split("/")[-1]
    name = re.sub(r"[\x00-\x1f\x7f]", "", name)
    return name[:200] or "unnamed"


def detect_format(data: bytes, filename: str | None, declared: str | None) -> str:
    for magic in ARCHIVE_MAGIC:
        if data.startswith(magic):
            raise AdmissionError("archive_not_supported", "archives are rejected by default")
    if len(data) > 262 and data[257:262] == b"ustar":
        raise AdmissionError("archive_not_supported", "tar archives are rejected")
    fmt = declared.lower() if declared else EXT.get(os.path.splitext((filename or "").lower())[1])
    if fmt is None:
        raise AdmissionError("unsupported_format", "declare a type or use .pdf/.json/.md/.sql")
    if fmt not in TYPE_NAMES:
        raise AdmissionError("unsupported_format", f"unsupported type {fmt!r}")
    is_pdf = data[:1024].lstrip(b"\x00\t\r\n ").startswith(b"%PDF-") or b"%PDF-" in data[:1024]
    if fmt == "pdf" and not is_pdf:
        raise AdmissionError("type_mismatch", "declared PDF has no %PDF- header")
    if fmt != "pdf" and data.startswith(b"%PDF-"):
        raise AdmissionError("type_mismatch", f"content is a PDF but declared {fmt}")
    if fmt != "pdf" and b"\x00" in data:
        raise AdmissionError("binary_content", "NUL bytes in a text format")
    return fmt


def admit(data: bytes, filename: str | None, declared: str | None, store_dir: Path,
          limits: AdmissionLimits = AdmissionLimits()) -> Admitted:
    if not data:
        raise AdmissionError("empty_file")
    if len(data) > limits.max_bytes:
        raise AdmissionError("size_limit", f"{len(data)} > {limits.max_bytes}")
    fmt = detect_format(data, filename, declared)
    digest = sha256_digest(data)
    store_blob(store_dir, digest, data)
    return Admitted(digest, len(data), fmt, safe_display_name(filename))


def blob_path(store_dir: Path, digest: str) -> Path:
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", digest)
    return store_dir / digest.split(":", 1)[1]


def store_blob(store_dir: Path, digest: str, data: bytes) -> Path:
    target = blob_path(store_dir, digest)
    if target.exists():
        return target
    store_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=store_dir, prefix=".stage-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o444)
        os.replace(tmp, target)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return target


def read_blob(store_dir: Path, digest: str) -> bytes:
    data = blob_path(store_dir, digest).read_bytes()
    if sha256_digest(data) != digest:
        raise AdmissionError("blob_digest_mismatch", digest)
    return data

"""Native-text PDF extraction (optional dependency: pypdf). No OCR, no rendering, no attachments."""
from __future__ import annotations

import io
import re

from ..common import (CORRUPT, ENCRYPTED, LIMIT, NOT_ATTEMPTED, PARSED, PARTIAL, UNSUPPORTED,
                      Extraction, Limits, make_fact, region)
from ..values import parse_plain_scalar

LINE_RE = re.compile(r"^\s*([a-z][a-z0-9_]*(?:\.[a-z0-9_]+|\[[0-9]+\])*)\s*[:=]\s*(\S.*?)\s*$")
MIN_TEXT = 5


def available() -> tuple[bool, str]:
    try:
        import pypdf
        return True, pypdf.__version__
    except Exception:
        return False, ""


def extract(data: bytes, limits: Limits) -> Extraction:
    ok, version = available()
    ext = Extraction("pdf", "pypdf", version or "unavailable")
    total = len(data)
    if not ok:
        ext.regions = [region("document", 0, total, NOT_ATTEMPTED, "NONE", ("missing_optional_parser",),
                              "install the 'pdf' extra (pypdf) to enable PDF extraction")]
        ext.diag("missing_optional_parser", parser="pypdf")
        return ext
    if total > limits.max_bytes:
        ext.regions = [region("document", 0, total, LIMIT, "NONE", ("byte_limit",))]
        return ext
    import logging
    import pypdf
    from pypdf.errors import PdfReadError
    logging.getLogger("pypdf").setLevel(logging.CRITICAL)  # parser diagnostics are recorded in the extraction record
    try:
        reader = pypdf.PdfReader(io.BytesIO(data), strict=False)
        if reader.is_encrypted:
            ext.regions = [region("document", 0, total, ENCRYPTED, "NONE", ("encrypted_no_password_attempt",))]
            ext.diag("encrypted")
            return ext
        page_count = len(reader.pages)
    except (PdfReadError, ValueError, KeyError, TypeError, OSError, AttributeError, RecursionError) as exc:
        ext.regions = [region("document", 0, total, CORRUPT, "NONE", ("unreadable_pdf",), type(exc).__name__)]
        ext.diag("pdf_unreadable", error=type(exc).__name__)
        return ext
    if page_count > limits.max_pages:
        ext.regions = [region("document", 0, total, LIMIT, "NONE", ("page_limit",), f"{page_count} pages")]
        ext.diag("resource_limit", error="page_limit", pages=page_count)
        return ext
    rendition = bytearray()
    page_ranges = []
    texts: list[str] = []
    page_status = []
    for idx in range(page_count):
        try:
            text = reader.pages[idx].extract_text() or ""
            text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x0c", "\n")
            status, reasons = ((PARSED, ()) if len(text.strip()) >= MIN_TEXT
                               else (UNSUPPORTED, ("no_text_layer_ocr_not_run",)))
        except Exception as exc:  # parser diagnostics are retained, never swallowed silently
            text, status, reasons = "", CORRUPT, ("page_extraction_failed",)
            ext.diag("page_extraction_failed", page_index=idx, error=type(exc).__name__)
        if sum(len(t) for t in texts) + len(text) > limits.max_text_chars:
            ext.diag("resource_limit", error="text_limit", page_index=idx)
            page_ranges.append((len(rendition), len(rendition), idx, LIMIT, ("text_limit",)))
            page_status.append(LIMIT)
            break
        start = len(rendition)
        rendition += text.encode("utf-8")
        page_ranges.append((start, len(rendition), idx, status, reasons))
        rendition += b"\x0c"
        texts.append(text)
        page_status.append(status)
    ext.rendition = bytes(rendition)
    ext.rendition_media_type = "text/plain; charset=utf-8"
    keys: dict[str, list[int]] = {}
    for start, end, idx, status, reasons in page_ranges:
        ext.regions.append(region("page", start, end, status,
                                  "FACTS_EXTRACTED" if status == PARSED else "NONE", reasons,
                                  f"page {idx + 1}"))
        if status != PARSED:
            continue
        pos = start
        for line in ext.rendition[start:end].decode("utf-8").split("\n"):
            lb = len(line.encode("utf-8"))
            m = LINE_RE.match(line)
            if m and not m.group(2).startswith("//") and len(ext.facts) < limits.max_facts:
                vstart = pos + len(line[:m.start(2)].encode("utf-8"))
                vend = pos + len(line[:m.end(2)].encode("utf-8"))
                ext.facts.append(make_fact(
                    m.group(1), parse_plain_scalar(m.group(2)), "plain-scalar", ext.rendition,
                    vstart, vend, {"kind": "pdf", "page_index": idx, "bbox": None,
                                   "coordinate_note": "no geometry recorded by this adapter"},
                    "pdf.native_text", context=(pos, pos + lb),
                    qualifiers=("pdf_reading_order_unverified",)))
                keys.setdefault(m.group(1), []).append(len(ext.facts) - 1)
            pos += lb + 1
    drop = set()
    for key, idxs in keys.items():
        vals = {(ext.facts[i]["value"]["kind"], str(ext.facts[i]["value"]["value"])) for i in idxs}
        if len(idxs) > 1:
            drop.update(idxs)
            ext.blocked_keys.append({"key": key, "reason": "repeated_key", "occurrences": [
                {"byte_start": ext.facts[i]["locator"]["byte_start"],
                 "byte_end": ext.facts[i]["locator"]["byte_end"]} for i in idxs]})
            ext.diag("repeated_key", key=key, distinct_values=len(vals))
    ext.facts = [f for i, f in enumerate(ext.facts) if i not in drop]
    return ext

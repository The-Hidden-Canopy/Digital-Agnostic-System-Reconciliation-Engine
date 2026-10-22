"""Inert Markdown: front matter and key/value tables yield facts; everything else is accounted for."""
from __future__ import annotations

import re

from ..common import (CORRUPT, LIMIT, PARSED, PARTIAL, UNSUPPORTED, Extraction, Limits,
                      finalize_regions, make_fact, region)
from ..values import parse_plain_scalar

KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.\-]*\Z")
FENCE_RE = re.compile(r"(`{3,}|~{3,})")
TEMPLATE_RE = re.compile(r"^\s*(!include\b|\{\{|\{%|<%|@include\b|<\?)")
KEY_COLS = {"key", "setting", "name", "parameter", "field", "property"}
VALUE_COLS = {"value", "expected", "default", "limit"}


def _lines(data: bytes) -> list[tuple[int, int, str]]:
    out, pos = [], 0
    for raw in data.split(b"\n"):
        end = pos + len(raw)
        out.append((pos, end, raw.decode("utf-8")))
        pos = end + 1
    if out and out[-1][0] == len(data):
        out.pop()
    return out


def _cells(line: str) -> list[tuple[int, int, str]]:
    """Split a table row on unescaped pipes; returns (char_start, char_end, text)."""
    cells, i, start = [], 0, 0
    s = line
    lead = len(s) - len(s.lstrip())
    i = start = lead
    if i < len(s) and s[i] == "|":
        i += 1
        start = i
    while i <= len(s):
        if i == len(s) or (s[i] == "|" and s[i - 1] != "\\"):
            cells.append((start, i, s[start:i]))
            start = i + 1
        i += 1
    if cells and cells[-1][2].strip() == "":
        cells.pop()
    return cells


def _trim(line: str, a: int, b: int) -> tuple[int, int]:
    while a < b and line[a].isspace():
        a += 1
    while b > a and line[b - 1].isspace():
        b -= 1
    return a, b


def extract(data: bytes, limits: Limits) -> Extraction:
    ext = Extraction("markdown", "verifier_lab.markdown_blocks", "1")
    total = len(data)
    if total > limits.max_bytes:
        ext.regions = [region("document", 0, total, LIMIT, "NONE", ("byte_limit",))]
        return ext
    try:
        data.decode("utf-8", errors="strict")
        lines = _lines(data)
    except UnicodeDecodeError as exc:
        ext.regions = [region("document", 0, total, CORRUPT, "NONE", ("invalid_utf8",))]
        ext.diag("invalid_utf8", byte_offset=exc.start)
        return ext
    regions: list[dict] = []
    facts: list[dict] = []
    seen_keys: dict[str, list[int]] = {}

    def add_fact(key, token_start, token_end, ctx, extra, method):
        if len(facts) >= limits.max_facts:
            return False
        text = data[token_start:token_end].decode("utf-8")
        facts.append(make_fact(key, parse_plain_scalar(text), "plain-scalar", data,
                               token_start, token_end, extra, method, context=ctx))
        seen_keys.setdefault(key, []).append(len(facts) - 1)
        return True

    i, n = 0, len(lines)
    if n and lines[0][2].rstrip() == "---":
        close = next((j for j in range(1, n) if lines[j][2].rstrip() in ("---", "...")), None)
        if close is None:
            regions.append(region("front_matter", lines[0][0], lines[0][1], UNSUPPORTED, "NONE",
                                  ("unterminated_front_matter",)))
            ext.diag("unterminated_front_matter", line=1)
            i = 1
        else:
            bad = _front_matter(lines, 1, close, data, add_fact, ext)
            regions.append(region("front_matter", lines[0][0], lines[close][1],
                                  PARTIAL if bad else PARSED, "FACTS_EXTRACTED",
                                  ("unsupported_front_matter_lines",) if bad else ()))
            i = close + 1
    while i < n:
        s, e, line = lines[i]
        stripped = line.strip()
        if not stripped:
            i += 1
            continue
        fence = FENCE_RE.match(stripped)
        if fence:
            marker = fence.group(1)
            j = i + 1
            while j < n and not lines[j][2].strip().startswith(marker[0] * len(marker)):
                j += 1
            closed = j < n
            end_line = j if closed else n - 1
            regions.append(region("code_fence", s, lines[end_line][1], PARSED, "NONCLAIM",
                                  ("code_not_executed",) if closed else ("code_not_executed", "unterminated_fence")))
            i = end_line + 1
            continue
        if TEMPLATE_RE.match(line):
            regions.append(region("directive", s, e, UNSUPPORTED, "NONE", ("template_directive_not_interpreted",)))
            i += 1
            continue
        if stripped.startswith("<"):
            j = i
            while j + 1 < n and lines[j + 1][2].strip():
                j += 1
            if stripped.startswith("<!--") and "-->" not in "".join(l[2] for l in lines[i:j + 1]):
                while j + 1 < n and "-->" not in lines[j][2]:
                    j += 1
            regions.append(region("raw_html", s, lines[j][1], UNSUPPORTED, "NONE", ("raw_html_not_interpreted",)))
            i = j + 1
            continue
        if "|" in line and i + 1 < n and re.match(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$", lines[i + 1][2]):
            j = i + 2
            while j < n and "|" in lines[j][2] and lines[j][2].strip():
                j += 1
            hdr = [c[2].strip().lower() for c in _cells(line)]
            kv = len(hdr) >= 2 and hdr[0] in KEY_COLS and hdr[1] in VALUE_COLS
            if kv:
                bad = False
                for r in range(i + 2, j):
                    rs, re_, rl = lines[r]
                    cs = _cells(rl)
                    if len(cs) < 2:
                        bad = True
                        continue
                    ka, kb = _trim(rl, cs[0][0], cs[0][1])
                    va, vb = _trim(rl, cs[1][0], cs[1][1])
                    key = rl[ka:kb].strip("`")
                    base = len(rl[:va].encode())
                    if not KEY_RE.match(key) or va == vb:
                        bad = True
                        continue
                    vs = rs + len(rl[:va].encode())
                    ve = rs + len(rl[:vb].encode())
                    if not add_fact(key, vs, ve, (rs, re_),
                                    {"kind": "markdown", "node": "table_row", "line": r + 1}, "markdown.table"):
                        bad = True
                regions.append(region("table", s, lines[j - 1][1], PARTIAL if bad else PARSED,
                                      "FACTS_EXTRACTED", ("unparsed_rows",) if bad else ()))
            else:
                regions.append(region("table", s, lines[j - 1][1], PARSED, "PROSE_UNSTRUCTURED",
                                      ("table_without_key_value_header",)))
            i = j
            continue
        kind = "heading" if stripped.startswith("#") else "list" if re.match(r"^([-*+]|\d+[.)])\s", stripped) \
            else "blockquote" if stripped.startswith(">") else "paragraph"
        j = i
        while j + 1 < n and lines[j + 1][2].strip() and not FENCE_RE.match(lines[j + 1][2].strip()) \
                and not lines[j + 1][2].strip().startswith(("#", "<")) and kind != "heading":
            j += 1
        regions.append(region(kind, s, lines[j][1], PARSED,
                              "STRUCTURE" if kind == "heading" else "PROSE_UNSTRUCTURED",
                              () if kind == "heading" else ("no_deterministic_claims",)))
        i = j + 1
    # duplicate keys: preserve occurrences, emit no semantic facts for them
    drop = set()
    for key, idxs in seen_keys.items():
        if len(idxs) > 1:
            drop.update(idxs)
            ext.blocked_keys.append({"key": key, "reason": "duplicate_key", "occurrences": [
                {"byte_start": facts[k]["locator"]["byte_start"], "byte_end": facts[k]["locator"]["byte_end"]}
                for k in idxs]})
            ext.diag("duplicate_key", key=key, occurrences=len(idxs))
    ext.facts = [f for k, f in enumerate(facts) if k not in drop]
    if drop:
        for r in regions:
            if r["interpretation"] == "FACTS_EXTRACTED" and r["status"] == PARSED:
                r["status"], r["reason_codes"] = PARTIAL, ["duplicate_keys_withheld"]
    ext.regions = finalize_regions(regions, data)
    return ext


def _front_matter(lines, lo, hi, data, add_fact, ext) -> bool:
    """Minimal YAML subset: nested `key: value` maps by 2+ space indentation. Returns True if lines were skipped."""
    bad = False
    stack: list[tuple[int, str]] = []
    for j in range(lo, hi):
        s, e, line = lines[j]
        if not line.strip() or line.strip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        m = re.match(r"^( *)([A-Za-z_][A-Za-z0-9_.\-]*):(?:\s+(.*?))?\s*$", line)
        if not m or "\t" in line[:indent]:
            bad = True
            ext.diag("unsupported_front_matter_line", line=j + 1)
            continue
        while stack and stack[-1][0] >= indent:
            stack.pop()
        key = ".".join([k for _, k in stack] + [m.group(2)])
        val = m.group(3)
        if val is None or val == "":
            stack.append((indent, m.group(2)))
            continue
        if val[:1] in "[{|>&*!" and val[:1] != "":
            bad = True
            ext.diag("unsupported_front_matter_value", line=j + 1)
            continue
        vstart = len(line[:m.start(3)].encode())
        vend = len(line[:m.end(3)].encode())
        if "#" in val and val[0] not in "\"'":
            val_clean = val.split(" #")[0].rstrip()
            vend = vstart + len(val_clean.encode())
        add_fact(key, s + vstart, s + vend, (s, e),
                 {"kind": "markdown", "node": "front_matter", "line": j + 1}, "markdown.front_matter")
    return bad

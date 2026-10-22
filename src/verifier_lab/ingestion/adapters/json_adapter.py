"""Strict JSON with lexical offsets, member occurrences and duplicate-key accounting (chapter 17)."""
from __future__ import annotations

import json
import re

from ..common import (CORRUPT, LIMIT, PARSED, PARTIAL, UNSUPPORTED, ByteMap, Extraction, Limits,
                      finalize_regions, make_fact, region)
from ..values import LiteralError, parse_json_literal

NUM_RE = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?")
WS = " \t\n\r"


class JsonSyntax(Exception):
    def __init__(self, code: str, offset: int):
        super().__init__(code)
        self.code, self.offset = code, offset


class _Stop(Exception):
    """Resource limit reached."""
    def __init__(self, code: str, offset: int):
        super().__init__(code)
        self.code, self.offset = code, offset


def _key_segment(k: str) -> str:
    if k and not any(c in k for c in '.[]"\\~') and k.isprintable() and k == k.strip():
        return k
    return "[" + json.dumps(k, ensure_ascii=False) + "]"


def _ptr_escape(k: str) -> str:
    return k.replace("~", "~0").replace("/", "~1")


class _Parser:
    def __init__(self, text: str, limits: Limits, bm: ByteMap, rendition: bytes):
        self.s, self.n, self.i = text, len(text), 0
        self.limits, self.bm, self.rendition = limits, bm, rendition
        self.tokens = 0
        self.facts: list[dict] = []
        self.blocked: list[dict] = []
        self.diags: list[dict] = []
        self.skipped = False

    def ws(self):
        while self.i < self.n and self.s[self.i] in WS:
            self.i += 1

    def tick(self):
        self.tokens += 1
        if self.tokens > self.limits.max_tokens:
            raise _Stop("token_limit", self.i)

    def value(self, key: str, ptr: str, depth: int, member_index: int, ctx_start: int) -> tuple[int, int, bool]:
        """Parse a value; returns (start, end, clean) in char offsets."""
        if depth > self.limits.max_depth:
            raise _Stop("depth_limit", self.i)
        self.ws()
        if self.i >= self.n:
            raise JsonSyntax("unexpected_end", self.i)
        start, c = self.i, self.s[self.i]
        self.tick()
        if c == "{":
            return self.obj(key, ptr, depth, start)
        if c == "[":
            return self.arr(key, ptr, depth, start)
        if c == '"':
            end = self.string()
            return self.scalar(key, ptr, start, end, member_index, ctx_start)
        for lit in ("true", "false", "null"):
            if self.s.startswith(lit, start):
                end = start + len(lit)
                self.i = end
                return self.scalar(key, ptr, start, end, member_index, ctx_start)
        m = NUM_RE.match(self.s, start)
        if m and m.end() > start:
            self.i = m.end()
            if self.i < self.n and self.s[self.i] not in WS + ",]}":
                raise JsonSyntax("bad_number", start)
            return self.scalar(key, ptr, start, self.i, member_index, ctx_start)
        raise JsonSyntax("unexpected_character", start)

    def string(self) -> int:
        j = self.i + 1
        while j < self.n:
            ch = self.s[j]
            if ch == "\\":
                if j + 1 >= self.n:
                    break
                if self.s[j + 1] == "u":
                    if not re.match(r"[0-9a-fA-F]{4}", self.s[j + 2:j + 6]):
                        raise JsonSyntax("bad_escape", j)
                    j += 6
                elif self.s[j + 1] in '"\\/bfnrt':
                    j += 2
                else:
                    raise JsonSyntax("bad_escape", j)
                continue
            if ch == '"':
                self.i = j + 1
                return self.i
            if ord(ch) < 0x20:
                raise JsonSyntax("control_character", j)
            j += 1
        raise JsonSyntax("unterminated_string", self.i)

    def scalar(self, key, ptr, start, end, member_index, ctx_start):
        token = self.s[start:end]
        try:
            val = parse_json_literal(token)
        except LiteralError:
            # exact numeric beyond supported digits: keep bytes, refuse conversion
            self.diags.append({"code": "numeric_limit", "pointer": ptr,
                               "byte_start": self.bm(start), "byte_end": self.bm(end)})
            self.skipped = True
            return start, end, False
        if len(self.facts) >= self.limits.max_facts:
            raise _Stop("fact_limit", start)
        bs, be = self.bm(start), self.bm(end)
        self.facts.append(make_fact(
            key, val, "json-literal", self.rendition, bs, be,
            {"kind": "json", "pointer": ptr, "member_index": member_index}, "json.scalar",
            context=(self.bm(ctx_start), be)))
        return start, end, True

    def obj(self, key, ptr, depth, start):
        self.i += 1
        members: list[tuple[str, int, int, int]] = []  # key, index, fact_lo, fact_hi
        clean = True
        self.ws()
        if self.i < self.n and self.s[self.i] == "}":
            self.i += 1
            return start, self.i, True
        idx = 0
        while True:
            self.ws()
            if self.i >= self.n:
                raise JsonSyntax("unexpected_end", self.i)
            if self.s[self.i] != '"':
                raise JsonSyntax("expected_key", self.i)
            kstart = self.i
            kend = self.string()
            k = json.loads(self.s[kstart:kend])
            self.ws()
            if self.i >= self.n or self.s[self.i] != ":":
                raise JsonSyntax("expected_colon", self.i)
            self.i += 1
            child_key = (key + "." if key else "") + _key_segment(k)
            lo = len(self.facts)
            _, vend, ok = self.value(child_key, ptr + "/" + _ptr_escape(k), depth + 1, idx, kstart)
            clean = clean and ok
            members.append((k, idx, lo, len(self.facts), kstart, vend))
            idx += 1
            self.ws()
            if self.i >= self.n:
                raise JsonSyntax("unexpected_end", self.i)
            if self.s[self.i] == ",":
                self.i += 1
                continue
            if self.s[self.i] == "}":
                self.i += 1
                break
            raise JsonSyntax("expected_comma_or_close", self.i)
        counts: dict[str, int] = {}
        for k, *_ in members:
            counts[k] = counts.get(k, 0) + 1
        dup = {k for k, c in counts.items() if c > 1}
        if dup:
            drop: set[int] = set()
            for k, mi, lo, hi, ks, ve in members:
                if k in dup:
                    drop.update(range(lo, hi))
            for k in sorted(dup):
                occ = [m for m in members if m[0] == k]
                ck = (key + "." if key else "") + _key_segment(k)
                self.blocked.append({"key": ck, "reason": "duplicate_key", "occurrences": [
                    {"member_index": m[1], "byte_start": self.bm(m[4]), "byte_end": self.bm(m[5])}
                    for m in occ]})
                self.diags.append({"code": "duplicate_key", "pointer": ptr + "/" + _ptr_escape(k),
                                   "occurrences": len(occ)})
            self.facts = [f for j, f in enumerate(self.facts) if j not in drop]
            clean = False
            self.skipped = True
        return start, self.i, clean

    def arr(self, key, ptr, depth, start):
        self.i += 1
        self.ws()
        clean = True
        if self.i < self.n and self.s[self.i] == "]":
            self.i += 1
            return start, self.i, True
        idx = 0
        while True:
            _, _, ok = self.value(f"{key}[{idx}]", f"{ptr}/{idx}", depth + 1, idx, self.i)
            clean = clean and ok
            idx += 1
            self.ws()
            if self.i >= self.n:
                raise JsonSyntax("unexpected_end", self.i)
            if self.s[self.i] == ",":
                self.i += 1
                continue
            if self.s[self.i] == "]":
                self.i += 1
                return start, self.i, clean
            raise JsonSyntax("expected_comma_or_close", self.i)


def extract(data: bytes, limits: Limits) -> Extraction:
    ext = Extraction("json", "verifier_lab.json_lexical", "1")
    total = len(data)
    if total > limits.max_bytes:
        ext.regions = [region("document", 0, total, LIMIT, "NONE", ("byte_limit",))]
        return ext
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        ext.regions = [region("document", 0, total, CORRUPT, "NONE", ("invalid_utf8",),
                              f"offset {exc.start}")]
        ext.diag("invalid_utf8", byte_offset=exc.start)
        return ext
    if text.startswith("﻿"):
        ext.regions = [region("document", 0, total, UNSUPPORTED, "NONE", ("bom_not_supported",))]
        return ext
    bm = ByteMap(text)
    p = _Parser(text, limits, bm, data)
    try:
        p.ws()
        start = p.i
        _, end, clean = p.value("", "", 0, 0, start)
        p.ws()
        if p.i != p.n:
            raise JsonSyntax("trailing_content", p.i)
    except JsonSyntax as exc:
        ext.regions = [region("document", 0, total, CORRUPT, "NONE", (exc.code,),
                              f"byte offset {bm(min(exc.offset, p.n))}")]
        ext.diag("syntax_error", error=exc.code, byte_offset=bm(min(exc.offset, p.n)))
        return ext
    except _Stop as exc:
        ext.regions = [region("document", 0, total, LIMIT, "NONE", (exc.code,))]
        ext.diag("resource_limit", error=exc.code)
        return ext
    ext.facts, ext.blocked_keys, ext.diagnostics = p.facts, p.blocked, p.diags
    # coverage: one region per top-level member (or the root value)
    regions: list[dict] = []
    fact_ranges = [(f["locator"]["byte_start"], f["locator"]["byte_end"]) for f in p.facts]
    blocked_ptrs = {d["pointer"] for d in p.diags if "pointer" in d}
    top_members = _top_members(text)
    if top_members is None:
        status = PARSED if clean and not p.skipped else PARTIAL
        regions.append(region("root_value", bm(start), bm(end), status,
                              "FACTS_EXTRACTED" if p.facts else "NONCLAIM",
                              () if status == PARSED else ("skipped_content",)))
    else:
        for kstart, vend, kname in top_members:
            bs, be = bm(kstart), bm(vend)
            has = any(bs <= a < be for a, _ in fact_ranges)
            bad = any(pp == "/" + _ptr_escape(kname) or pp.startswith("/" + _ptr_escape(kname) + "/")
                      for pp in blocked_ptrs)
            regions.append(region("member", bs, be, PARTIAL if bad else PARSED,
                                  "FACTS_EXTRACTED" if has else "NONCLAIM",
                                  ("duplicate_or_unconvertible_value",) if bad else ()))
    ext.regions = finalize_regions(regions, data)
    return ext


def _top_members(text: str):
    """Re-scan the root object for member extents (cheap; text already validated)."""
    try:
        p = _Parser(text, Limits(), ByteMap(text), b"")
        p.ws()
        if p.i >= p.n or text[p.i] != "{":
            return None
        p.i += 1
        out = []
        p.ws()
        if text[p.i] == "}":
            return out
        while True:
            p.ws()
            ks = p.i
            ke = p.string()
            k = json.loads(text[ks:ke])
            p.ws()
            p.i += 1
            p2 = _Parser(text, Limits(), ByteMap(text), b"")
            p2.i = p.i
            _, ve, _ = p2.value("", "", 1, 0, ks)
            out.append((ks, ve, k))
            p.i = p2.i
            p.ws()
            if text[p.i] == ",":
                p.i += 1
                continue
            return out
    except Exception:
        return None

"""Static SQL: lexical statement splitting and a narrow CREATE TABLE / INSERT reader. Never executes."""
from __future__ import annotations

import re

from ..common import (CORRUPT, LIMIT, PARSED, PARTIAL, UNSUPPORTED, ByteMap, Extraction, Limits,
                      finalize_regions, make_fact, region)
from ..values import parse_plain_scalar, parse_sql_literal

DIALECTS = ("generic", "postgresql", "mysql", "sqlite", "unresolved")
ID_START = re.compile(r"[A-Za-z_\u0080-￿]")
ID_PART = re.compile(r"[A-Za-z0-9_$\u0080-￿]")
CONSTRAINT_KW = {"NOT", "NULL", "DEFAULT", "PRIMARY", "REFERENCES", "UNIQUE", "CHECK",
                 "CONSTRAINT", "COLLATE", "GENERATED", "AUTO_INCREMENT", "AUTOINCREMENT", "COMMENT"}
TABLE_KW = {"CONSTRAINT", "PRIMARY", "FOREIGN", "UNIQUE", "CHECK", "KEY", "INDEX", "EXCLUDE", "LIKE"}


class Tok:
    __slots__ = ("kind", "text", "start", "end")

    def __init__(self, kind, text, start, end):
        self.kind, self.text, self.start, self.end = kind, text, start, end

    def up(self):
        return self.text.upper() if self.kind == "word" else self.text


def tokenize(text: str, dialect: str, max_tokens: int):
    toks: list[Tok] = []
    comments: list[tuple[int, int]] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c.isspace():
            i += 1
        elif text.startswith("--", i) or (dialect == "mysql" and c == "#"):
            j = text.find("\n", i)
            j = n if j < 0 else j
            comments.append((i, j))
            i = j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            if j < 0:
                raise ValueError(("unterminated_comment", i))
            comments.append((i, j + 2))
            i = j + 2
        elif c == "'":
            j = i + 1
            while True:
                k = text.find("'", j)
                if k < 0:
                    raise ValueError(("unterminated_string", i))
                if text.startswith("''", k):
                    j = k + 2
                    continue
                if dialect == "mysql" and k > 0 and text[k - 1] == "\\":
                    j = k + 1
                    continue
                break
            toks.append(Tok("string", text[i:k + 1], i, k + 1))
            i = k + 1
        elif c == '"' or c == "`" or c == "[" and dialect == "sqlite":
            close = {'"': '"', "`": "`", "[": "]"}[c]
            j = i + 1
            buf = []
            while True:
                k = text.find(close, j)
                if k < 0:
                    raise ValueError(("unterminated_identifier", i))
                if close != "]" and text.startswith(close * 2, k):
                    buf.append(text[j:k + 1])
                    j = k + 2
                    continue
                buf.append(text[j:k])
                break
            toks.append(Tok("qident", "".join(buf), i, k + 1))
            i = k + 1
        elif c == "$" and dialect == "postgresql" and re.match(r"\$[A-Za-z_]*\$", text[i:]):
            tag = re.match(r"\$[A-Za-z_]*\$", text[i:]).group(0)
            k = text.find(tag, i + len(tag))
            if k < 0:
                raise ValueError(("unterminated_dollar_quote", i))
            toks.append(Tok("string", text[i:k + len(tag)], i, k + len(tag)))
            i = k + len(tag)
        elif ID_START.match(c):
            j = i + 1
            while j < n and ID_PART.match(text[j]):
                j += 1
            toks.append(Tok("word", text[i:j], i, j))
            i = j
        elif c.isdigit() or (c == "." and i + 1 < n and text[i + 1].isdigit()):
            m = re.compile(r"(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?").match(text, i)
            toks.append(Tok("number", m.group(0), i, m.end()))
            i = m.end()
        else:
            toks.append(Tok("punct", c, i, i + 1))
            i += 1
        if len(toks) > max_tokens:
            raise ValueError(("token_limit", i))
    return toks, comments


def split_statements(toks: list[Tok]) -> list[list[Tok]]:
    out, cur = [], []
    for t in toks:
        if t.kind == "punct" and t.text == ";":
            if cur:
                out.append(cur)
            cur = []
        else:
            cur.append(t)
    if cur:
        out.append(cur)
    return out


def _ident(tok: Tok, dialect: str) -> str:
    if tok.kind == "qident":
        return tok.text
    return tok.text if dialect == "unresolved" else tok.text.lower()


def _qualified(stmt, i, dialect):
    """Read possibly schema-qualified name starting at i; returns (name, next_index)."""
    parts = []
    while i < len(stmt) and stmt[i].kind in ("word", "qident"):
        parts.append(_ident(stmt[i], dialect))
        i += 1
        if i < len(stmt) and stmt[i].text == "." and stmt[i].kind == "punct":
            i += 1
            continue
        break
    return ".".join(parts), i


def _paren_groups(stmt, i):
    """stmt[i] is '(' -> list of token lists split on top-level commas, and index after ')'."""
    depth, groups, cur = 0, [], []
    j = i
    while j < len(stmt):
        t = stmt[j]
        if t.kind == "punct" and t.text == "(":
            depth += 1
            if depth > 1:
                cur.append(t)
        elif t.kind == "punct" and t.text == ")":
            depth -= 1
            if depth == 0:
                groups.append(cur)
                return groups, j + 1
            cur.append(t)
        elif t.kind == "punct" and t.text == "," and depth == 1:
            groups.append(cur)
            cur = []
        else:
            cur.append(t)
        j += 1
    return None, j


def extract(data: bytes, limits: Limits, dialect: str | None) -> Extraction:
    ext = Extraction("sql", "verifier_lab.sql_static", "1")
    dialect = dialect or "unresolved"
    ext.dialect = dialect
    total = len(data)
    if dialect not in DIALECTS:
        ext.regions = [region("document", 0, total, UNSUPPORTED, "NONE", ("unsupported_dialect",), dialect)]
        ext.diag("unsupported_dialect", dialect=dialect)
        return ext
    if total > limits.max_bytes:
        ext.regions = [region("document", 0, total, LIMIT, "NONE", ("byte_limit",))]
        return ext
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        ext.regions = [region("document", 0, total, CORRUPT, "NONE", ("invalid_utf8",))]
        ext.diag("invalid_utf8", byte_offset=exc.start)
        return ext
    bm = ByteMap(text)
    try:
        toks, comments = tokenize(text, dialect, limits.max_tokens)
    except ValueError as exc:
        code, off = exc.args[0]
        status = LIMIT if code == "token_limit" else CORRUPT
        ext.regions = [region("document", 0, total, status, "NONE", (code,), f"byte offset {bm(off)}")]
        ext.diag("lexical_error", error=code, byte_offset=bm(off))
        return ext
    qual = ("dialect_unresolved",) if dialect == "unresolved" else ()
    regions = [region("comment", bm(a), bm(b), PARSED, "NONCLAIM", ("comment_not_authoritative",))
               for a, b in comments]
    facts: list[dict] = []
    seen: dict[str, list[int]] = {}
    row_counters: dict[str, int] = {}
    stmts = split_statements(toks)
    if len(stmts) > limits.max_statements:
        stmts = stmts[:limits.max_statements]
        ext.diag("statement_limit")
    # trailing text after last semicolon without a semicolon is still a statement (handled by split)

    def add(key, tok_start, tok_end, grammar, extra, method):
        if len(facts) >= limits.max_facts:
            return
        raw = text[tok_start:tok_end]
        v = (parse_plain_scalar(raw) if grammar == "plain-scalar"
             else parse_sql_literal(raw) if grammar == "sql-literal" else {"kind": "boolean", "value": True})
        facts.append(make_fact(key, v, grammar, data, bm(tok_start), bm(tok_end), extra, method,
                               qualifiers=qual + ("asserted_in_file_only",)))
        seen.setdefault(key, []).append(len(facts) - 1)

    for idx, st in enumerate(stmts):
        s0, s1 = bm(st[0].start), bm(st[-1].end)
        words = [t.up() for t in st[:6] if t.kind == "word"]
        first = words[0] if words else ""
        loc = {"kind": "sql", "statement_index": idx}
        if first == "CREATE" and "TABLE" in words[:4] and not ({"FUNCTION", "TRIGGER", "PROCEDURE"} & set(words)):
            ok, reasons = _create_table(st, dialect, add, loc)
            regions.append(region("statement:schema_definition", s0, s1, PARSED if ok else PARTIAL,
                                  "FACTS_EXTRACTED", tuple(reasons) or ("semantics_partially_supported",) if reasons else ()))
        elif first == "INSERT":
            ok, reasons = _insert(st, dialect, add, loc, row_counters)
            regions.append(region("statement:data_assertion", s0, s1, PARSED if ok else PARTIAL,
                                  "FACTS_EXTRACTED" if ok else "NONCLAIM", tuple(reasons)))
        elif first in ("SELECT", "WITH"):
            regions.append(region("statement:query_definition", s0, s1, PARSED, "NONCLAIM",
                                  ("query_describes_not_results",)))
        elif first in ("BEGIN", "COMMIT", "ROLLBACK", "SET", "START", "PRAGMA", "USE", "SAVEPOINT", "RELEASE"):
            regions.append(region("statement:session_config", s0, s1, PARSED, "NONCLAIM", ("session_config_ignored",)))
        else:
            regions.append(region("statement:unsupported", s0, s1, UNSUPPORTED, "NONE",
                                  ("unsupported_or_procedural_sql",), first or "unknown"))
    drop = set()
    for key, idxs in seen.items():
        if len(idxs) > 1:
            drop.update(idxs)
            ext.blocked_keys.append({"key": key, "reason": "duplicate_definition", "occurrences": [
                {"byte_start": facts[k]["locator"]["byte_start"], "byte_end": facts[k]["locator"]["byte_end"]}
                for k in idxs]})
            ext.diag("duplicate_definition", key=key, occurrences=len(idxs))
    ext.facts = [f for k, f in enumerate(facts) if k not in drop]
    ext.regions = finalize_regions(regions, data, structural=set(" \t\r\n;"))
    return ext


def _create_table(st, dialect, add, loc):
    reasons: list[str] = []
    i = 1
    while i < len(st) and st[i].up() in ("TEMP", "TEMPORARY", "UNLOGGED", "GLOBAL", "LOCAL"):
        i += 1
    if not (i < len(st) and st[i].up() == "TABLE"):
        return False, ["unsupported_create_form"]
    i += 1
    if [t.up() for t in st[i:i + 3]] == ["IF", "NOT", "EXISTS"]:
        i += 3
    table, i = _qualified(st, i, dialect)
    if not table or i >= len(st) or st[i].text != "(":
        return False, ["unsupported_create_form"]
    groups, _ = _paren_groups(st, i)
    if groups is None:
        return False, ["unbalanced_parentheses"]
    for g in groups:
        if not g:
            continue
        head = g[0].up()
        if g[0].kind == "word" and head in TABLE_KW:
            reasons.append("table_constraint_not_interpreted")
            continue
        if g[0].kind not in ("word", "qident"):
            reasons.append("unrecognized_column_definition")
            continue
        col = _ident(g[0], dialect)
        base = f"sql.table.{table}.column.{col}"
        k, type_toks = 1, []
        while k < len(g) and not (g[k].kind == "word" and g[k].up() in CONSTRAINT_KW):
            type_toks.append(g[k])
            k += 1
        if type_toks:
            add(base + ".type", type_toks[0].start, type_toks[-1].end, "plain-scalar",
                {**loc, "table": table, "column": col}, "sql.column_type")
        while k < len(g):
            if g[k].kind == "word" and g[k].up() == "NOT" and k + 1 < len(g) and g[k + 1].up() == "NULL":
                add(base + ".not_null", g[k].start, g[k + 1].end, "sql-flag",
                    {**loc, "table": table, "column": col}, "sql.not_null")
                k += 2
            elif g[k].kind == "word" and g[k].up() == "PRIMARY" and k + 1 < len(g) and g[k + 1].up() == "KEY":
                add(base + ".primary_key", g[k].start, g[k + 1].end, "sql-flag",
                    {**loc, "table": table, "column": col}, "sql.primary_key")
                k += 2
            elif g[k].kind == "word" and g[k].up() == "UNIQUE":
                add(base + ".unique", g[k].start, g[k].end, "sql-flag",
                    {**loc, "table": table, "column": col}, "sql.unique")
                k += 1
            else:
                k += 1
    return not reasons, reasons


def _insert(st, dialect, add, loc, row_counters):
    i = 1
    if i < len(st) and st[i].up() == "INTO":
        i += 1
    table, i = _qualified(st, i, dialect)
    if not table:
        return False, ["unsupported_insert_form"]
    cols = None
    if i < len(st) and st[i].text == "(":
        groups, i = _paren_groups(st, i)
        if groups is None or any(len(g) != 1 or g[0].kind not in ("word", "qident") for g in groups):
            return False, ["unsupported_insert_form"]
        cols = [_ident(g[0], dialect) for g in groups]
    if i >= len(st) or st[i].up() != "VALUES" or cols is None:
        return False, ["insert_without_column_list_or_values_not_interpreted"]
    i += 1
    while i < len(st):
        if st[i].text != "(":
            return False, ["unsupported_insert_form"]
        groups, i = _paren_groups(st, i)
        if groups is None or len(groups) != len(cols) or any(len(g) != 1 for g in groups):
            return False, ["row_not_literal"]
        row = row_counters.get(table, 0)
        row_counters[table] = row + 1
        for c, g in zip(cols, groups):
            tok = g[0]
            if tok.kind == "string":
                if tok.text.startswith("$"):
                    return False, ["row_not_literal"]
                add(f"sql.table.{table}.row[{row}].{c}", tok.start, tok.end, "sql-literal",
                    {**loc, "table": table, "column": c, "row": row}, "sql.insert_literal")
            elif tok.kind == "number" or (tok.kind == "word" and tok.up() in ("NULL", "TRUE", "FALSE")):
                add(f"sql.table.{table}.row[{row}].{c}", tok.start, tok.end, "sql-literal",
                    {**loc, "table": table, "column": c, "row": row}, "sql.insert_literal")
            else:
                return False, ["row_not_literal"]
        if i < len(st) and st[i].text == ",":
            i += 1
    return True, []

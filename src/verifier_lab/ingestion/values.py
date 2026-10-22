"""Exact typed scalar values shared by every adapter (chapter 17: exact numerics as tagged strings)."""
from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation

INT_RE = re.compile(r"-?(?:0|[1-9][0-9]*)\Z")
DEC_RE = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?\Z")
MAX_DIGITS = 100
MAX_EXPONENT = 1000

KINDS = ("string", "integer", "decimal", "boolean", "null")


def make_value(kind: str, value) -> dict:
    return {"kind": kind, "value": value}


def _number(token: str) -> dict | None:
    if INT_RE.match(token):
        if len(token.lstrip("-")) > MAX_DIGITS:
            return None
        return make_value("integer", token)
    if DEC_RE.match(token):
        digits = re.sub(r"[^0-9]", "", token.split("e")[0].split("E")[0])
        if len(digits) > MAX_DIGITS:
            return None
        if "e" in token.lower() and abs(int(re.split("[eE]", token)[1])) > MAX_EXPONENT:
            return None
        return make_value("decimal", token)
    return None


def parse_plain_scalar(text: str) -> dict:
    """Plain-scalar grammar used by Markdown, SQL type tokens and PDF lines."""
    t = text.strip()
    if len(t) >= 2 and t[0] == t[-1] and t[0] in "\"'":
        return make_value("string", t[1:-1])
    if t == "true":
        return make_value("boolean", True)
    if t == "false":
        return make_value("boolean", False)
    if t == "null":
        return make_value("null", None)
    return _number(t) or make_value("string", t)


def parse_sql_literal(token: str) -> dict:
    """SQL literal: quoted string with '' escape, case-insensitive NULL/TRUE/FALSE, exact numerics."""
    t = token.strip()
    if len(t) >= 2 and t[0] == t[-1] == "'":
        return make_value("string", t[1:-1].replace("''", "'"))
    up = t.upper()
    if up == "NULL":
        return make_value("null", None)
    if up in ("TRUE", "FALSE"):
        return make_value("boolean", up == "TRUE")
    return _number(t) or make_value("string", t)


class LiteralError(ValueError):
    pass


def parse_json_literal(token: str) -> dict:
    """Strict JSON scalar token -> typed value. Containers are not scalar facts."""
    if token == "true":
        return make_value("boolean", True)
    if token == "false":
        return make_value("boolean", False)
    if token == "null":
        return make_value("null", None)
    if token.startswith('"'):
        try:
            s = json.loads(token)
        except ValueError as exc:
            raise LiteralError("bad_string") from exc
        if not isinstance(s, str):
            raise LiteralError("bad_string")
        return make_value("string", s)
    n = _number(token)
    if n is None:
        raise LiteralError("bad_number")
    return n


def is_numeric(v: dict) -> bool:
    return v["kind"] in ("integer", "decimal")


def to_decimal(v: dict) -> Decimal:
    try:
        return Decimal(v["value"])
    except (InvalidOperation, TypeError) as exc:
        raise LiteralError("bad_decimal") from exc


def compare_equal(a: dict, b: dict) -> bool | None:
    """True/False when comparable, None on kind mismatch (needs human review)."""
    if is_numeric(a) and is_numeric(b):
        return to_decimal(a) == to_decimal(b)
    if a["kind"] != b["kind"]:
        return None
    return a["value"] == b["value"]


def display(v: dict) -> str:
    if v["kind"] == "null":
        return "null"
    if v["kind"] == "boolean":
        return "true" if v["value"] else "false"
    return str(v["value"])

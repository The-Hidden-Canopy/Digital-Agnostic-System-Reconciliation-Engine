"""Compact in-memory records for whole-repository scans.

A large repository has hundreds of thousands of written calls. As dictionaries each one costs
several hundred bytes before its strings; as a slotted mapping it costs about half and shares
its repeated strings. The mapping answers the same keys the dictionary did, so readers do not
change. These objects are for the desktop process only and are never serialised.
"""
from __future__ import annotations

import sys
from collections.abc import MutableMapping

_FIELDS = ("written", "written_truncated", "written_arguments", "path", "line", "snippet", "snippet_truncated",
           "line_sha256", "sha256", "evidence", "resolution", "target_id", "basis", "unresolved_form", "map_node_id",
           # as read by the parser, used while linking
           "caller", "base", "attribute", "member", "member_of", "scope", "local", "local_target", "expands")
_KNOWN = frozenset(_FIELDS)
_SHARED = ("written", "base", "attribute", "member", "member_of", "scope")
_NODE_SHARED = ("kind", "evidence", "language")
_SHAPE_SHARED = ("parameter_detail", "return_detail")


class CallRecord(MutableMapping):
    """One written call: the parser's reading, then its place in the scan. Unset keys are absent, as in a dict."""
    __slots__ = _FIELDS
    __hash__ = None

    def __init__(self, values=()):
        for key, value in dict(values).items():
            self[key] = value

    def __getitem__(self, key):
        if key in _KNOWN:
            try:
                return getattr(self, key)
            except AttributeError:
                pass
        raise KeyError(key)

    def __setitem__(self, key, value):
        if key not in _KNOWN:
            raise KeyError(key)
        setattr(self, key, value)

    def __delitem__(self, key):
        if key in _KNOWN:
            try:
                delattr(self, key)
                return
            except AttributeError:
                pass
        raise KeyError(key)

    def __iter__(self):
        return (key for key in _FIELDS if hasattr(self, key))

    def __len__(self):
        return sum(1 for _ in self)

    def get(self, key, default=None):
        return getattr(self, key, default) if key in _KNOWN else default

    def __repr__(self):
        return "CallRecord(" + repr(dict(self)) + ")"


def compact_parsed(parsed: dict, path: str) -> None:
    """Replace a file's parser output with compact, string-sharing records, in place."""
    ids = {}
    for node in parsed["nodes"]:
        ids[node["id"]] = node["id"]
        for key in _NODE_SHARED:
            if isinstance(node.get(key), str):
                node[key] = sys.intern(node[key])
        shape = node.get("shape")
        if shape:
            for key in _SHAPE_SHARED:
                if isinstance(shape.get(key), str):
                    shape[key] = sys.intern(shape[key])
            for item in shape.get("parameters", ()):
                item["kind"] = sys.intern(item["kind"])
            for item in shape.get("features", ()):
                item["kind"] = sys.intern(item["kind"])
    file_id = "file:" + path
    for edge in parsed["edges"]:
        edge["source"] = ids.get(edge["source"], file_id if edge["source"] == file_id else edge["source"])
        edge["target"] = ids.get(edge["target"], edge["target"])
        edge["kind"], edge["evidence"], edge["path"] = sys.intern(edge["kind"]), sys.intern(edge["evidence"]), path
    for item in parsed["imports"]:
        item["id"] = ids.get(item["id"], item["id"])
    lines, calls = {}, []
    for call in parsed["calls"]:
        record = CallRecord()
        for key, value in call.items():
            if key == "line_sha256" and not call.get("snippet_truncated"):
                continue  # recomputed from the snippet when a map item needs it
            if key in _SHARED and isinstance(value, str):
                value = sys.intern(value)
            elif key == "snippet":
                value = lines.setdefault(call["line"], value)  # calls written on one line share its text
            elif key == "caller":
                value = ids.get(value, file_id if value == file_id else value)
            elif key == "local_target":
                value = ids.get(value, value)
            record[key] = value
        calls.append(record)
    parsed["calls"] = calls

"""Experimental checks on synthetic per-file parser results, not parser attestation.

This module is deliberately excluded from the desktop bundle.  Matching a
snippet or digest to input bytes does not establish that a parser interpreted
those bytes correctly, and it does not make the transport an isolation boundary.
"""
from __future__ import annotations

import hashlib
import re

from .extract import ExtractionFailure

_LANGUAGES = {"repository-python": "python", "repository-javascript": "javascript",
              "repository-typescript": "typescript", "repository-tsx": "tsx",
              "repository-jsx": "jsx", "repository-cpp": "cpp"}
_ERRORS = {"invalid or overly complex Python source", "JavaScript parser unavailable; install the javascript extra",
           "unsupported or invalid JavaScript syntax", "language parser unavailable; install the languages extra",
           "invalid or overly complex source"}
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


def validate_graph_result(result, fmt, data, path):
    """Validate per-file wire structure and source excerpts before using them.

    Accepted shape is deliberately conservative. This is not a semantic AST
    verification and must only be used in the synthetic exchange experiment.
    """
    def reject():
        raise ExtractionFailure("worker_protocol_error")

    def fields(value, required, optional=()):
        if not isinstance(value, dict) or not set(required) <= value.keys() or value.keys() - set(required) - set(optional):
            reject()

    def string(value, nullable=False):
        if value is None and nullable:
            return
        if not isinstance(value, str) or len(value) > 200_000:
            reject()
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            reject()

    def number(value, minimum=0, maximum=200_000):
        if type(value) is not int or not minimum <= value <= maximum:
            reject()

    def boolean(value):
        if type(value) is not bool:
            reject()

    def sequence(value, maximum):
        if not isinstance(value, list) or len(value) > maximum:
            reject()

    if fmt not in _LANGUAGES or not isinstance(data, bytes) or not isinstance(path, str) or not path:
        reject()
    if isinstance(result, dict) and set(result) == {"error"}:
        if not isinstance(result["error"], str) or result["error"] not in _ERRORS:
            reject()
        return result
    required = {"nodes", "edges", "imports", "calls", "top_level", "truncated"}
    if fmt != "repository-python": required.add("exports")
    if fmt in {"repository-typescript", "repository-tsx", "repository-jsx", "repository-cpp"}: required.add("partial")
    fields(result, required)
    boolean(result["truncated"])
    if "partial" in result: boolean(result["partial"])
    try:
        lines = data.decode("utf-8-sig").splitlines()
    except UnicodeDecodeError:
        reject()
    digest, file_id = hashlib.sha256(data).hexdigest(), "file:" + path

    def line(value):
        number(value, 1, len(lines))

    def source_line(value, line_number, limit=1000):
        string(value); line(line_number)
        if value != lines[line_number - 1][:limit]: reject()

    def hash_value(value):
        if not isinstance(value, str) or not _DIGEST.fullmatch(value): reject()

    def shape(value, start, end):
        fields(value, {"parameters", "parameters_truncated", "parameter_detail", "async", "return_lines",
                       "returns_truncated", "return_detail", "features", "features_truncated", "evidence_lines"})
        for key in ("parameters_truncated", "returns_truncated", "features_truncated"): boolean(value[key])
        if value["async"] is not None: boolean(value["async"])
        if value["parameter_detail"] not in ("named", "named_or_pattern"): reject()
        if value["return_detail"] not in ("explicit", "explicit_only_arrow_expressions_excluded"): reject()
        sequence(value["parameters"], 30); sequence(value["return_lines"], 30)
        sequence(value["features"], 30); sequence(value["evidence_lines"], 20)
        for param in value["parameters"]:
            fields(param, {"name", "kind", "line"}); string(param["name"])
            if param["kind"] not in ("named", "pattern", "positional-only", "positional-or-keyword",
                                     "variadic positional", "keyword-only", "variadic keyword"): reject()
            line(param["line"])
            if not start <= param["line"] <= end: reject()
        for item in value["return_lines"]:
            line(item)
            if not start <= item <= end: reject()
        for item in value["features"]:
            fields(item, {"kind", "line"})
            if item["kind"] not in ("conditional", "loop", "selection", "exception handling", "raise", "await", "pattern match"): reject()
            line(item["line"])
            if not start <= item["line"] <= end: reject()
        for item in value["evidence_lines"]:
            fields(item, {"line", "text", "truncated"}); line(item["line"]); boolean(item["truncated"])
            source_line(item["text"], item["line"], 500)
            if item["truncated"] != (len(lines[item["line"] - 1]) > 500): reject()

    for key, limit in (("nodes", 600), ("edges", 600), ("imports", 3000), ("calls", 3000)):
        sequence(result[key], limit)
    ids = {}
    for node in result["nodes"]:
        fields(node, {"id", "kind", "name", "path", "line", "sha256", "evidence"},
               {"end_line", "snippet", "fingerprint", "language", "shape"})
        for key in ("id", "name", "path"): string(node[key])
        if node["id"] in ids or node["path"] != path or node["sha256"] != digest or node["evidence"] != "static": reject()
        line(node["line"])
        if node["kind"] not in ("function", "dependency") or not node["id"].startswith(("function:" if node["kind"] == "function" else "import:") + path + ":"): reject()
        if node["kind"] == "function":
            fields(node, {"id", "kind", "name", "path", "line", "end_line", "snippet", "sha256", "fingerprint", "language", "shape", "evidence"})
            line(node["end_line"])
            if node["end_line"] < node["line"] or node["language"] != _LANGUAGES[fmt]: reject()
            string(node["snippet"]); hash_value(node["fingerprint"])
            expected = "\n".join(lines[i - 1][:1000] for i in range(node["line"], min(node["end_line"], node["line"] + 11) + 1))
            if node["snippet"] != expected: reject()
            shape(node["shape"], node["line"], node["end_line"])
        else:
            fields(node, {"id", "kind", "name", "path", "line", "sha256", "evidence"}, {"language"})
            if "language" in node and node["language"] != _LANGUAGES[fmt]: reject()
        ids[node["id"]] = node

    targets = set()
    for edge in result["edges"]:
        fields(edge, {"source", "target", "kind", "evidence", "path", "line"})
        for key in ("source", "target"): string(edge[key])
        line(edge["line"])
        target = ids.get(edge["target"]); source = ids.get(edge["source"])
        if not target or edge["path"] != path or edge["evidence"] != "static" or edge["line"] != target["line"]: reject()
        if edge["source"] != file_id and (not source or source["kind"] != "function"): reject()
        if target["kind"] == "function" and edge["kind"] != "defines": reject()
        if target["kind"] == "dependency" and (edge["source"] != file_id or edge["kind"] != ("includes" if fmt == "repository-cpp" else "imports")): reject()
        if edge["target"] in targets: reject()
        targets.add(edge["target"])
    if targets != ids.keys(): reject()

    for imp in result["imports"]:
        common = {"id", "line", "snippet"}
        required_import = ({"module", "level", "symbol", "bound", "aliased"} if fmt == "repository-python" else
                           {"source", "local"} if fmt == "repository-cpp" else {"source", "bound", "symbol", "namespace"})
        fields(imp, common | required_import); string(imp["id"])
        node = ids.get(imp["id"])
        if not node or node["kind"] != "dependency" or node["line"] != imp["line"]: reject()
        source_line(imp["snippet"], imp["line"])
        if fmt == "repository-python":
            string(imp["module"]); number(imp["level"]); boolean(imp["aliased"])
            for key in ("symbol", "bound"): string(imp[key], nullable=True)
        else:
            string(imp["source"])
            if fmt == "repository-cpp": boolean(imp["local"])
            else:
                boolean(imp["namespace"])
                for key in ("symbol", "bound"): string(imp[key], nullable=True)

    for call in result["calls"]:
        fields(call, {"caller", "base", "attribute", "written", "written_truncated", "written_arguments", "line", "snippet", "snippet_truncated", "line_sha256"})
        string(call["caller"]); string(call["written"])
        for key in ("base", "attribute"): string(call[key], nullable=True)
        if call["caller"] != file_id and (call["caller"] not in ids or ids[call["caller"]]["kind"] != "function"): reject()
        if len(call["written"]) > 200: reject()
        if call["written_arguments"] is not None: number(call["written_arguments"])
        boolean(call["written_truncated"]); boolean(call["snippet_truncated"])
        source_line(call["snippet"], call["line"])
        source = lines[call["line"] - 1]
        if call["snippet_truncated"] != (len(source) > 1000) or call["line_sha256"] != hashlib.sha256(source.encode("utf-8")).hexdigest(): reject()

    for key in ("top_level", "exports"):
        if key not in result: continue
        mapping = result[key]
        if not isinstance(mapping, dict) or len(mapping) > 600: reject()
        for name, references in mapping.items():
            string(name); sequence(references, 600)
            for reference in references:
                string(reference)
                if reference not in ids or ids[reference]["kind"] != "function": reject()
    return result

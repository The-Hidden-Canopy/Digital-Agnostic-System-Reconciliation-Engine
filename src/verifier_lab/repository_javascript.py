"""JavaScript AST projection for the bounded repository worker."""
from __future__ import annotations

import hashlib


def parse_javascript(data, path):
    try:
        import esprima
    except ImportError:
        return {"error": "JavaScript parser unavailable; install the javascript extra"}
    try:
        text = data.decode("utf-8-sig")
        tree = esprima.parseModule(text, {"loc": True, "range": True}).toDict()
        visitor = _Visitor(path, text, hashlib.sha256(data).hexdigest())
        visitor.visit(tree)
        visitor.finish_exports()
        return {"nodes": visitor.nodes, "edges": visitor.edges, "imports": visitor.imports,
                "calls": visitor.calls, "top_level": visitor.top_level, "exports": visitor.exports,
                "truncated": visitor.truncated}
    except (UnicodeError, ValueError, RecursionError, MemoryError, esprima.Error):
        return {"error": "unsupported or invalid JavaScript syntax"}


def _shape(body, lines):
    from .repository_learning import evidence_lines
    params = body.get("params") or []
    names = []
    for param in params[:30]:
        if param.get("type") == "Identifier":
            names.append({"name": param["name"], "kind": "named", "line": param["loc"]["start"]["line"]})
        else:
            names.append({"name": "[pattern]", "kind": "pattern", "line": param["loc"]["start"]["line"]})
    returns, features = [], []
    def walk(n):
        if isinstance(n, list):
            for item in n:
                walk(item)
        elif isinstance(n, dict):
            typ = n.get("type")
            if typ in ("FunctionDeclaration", "FunctionExpression", "ArrowFunctionExpression", "ClassDeclaration", "ClassExpression"):
                return
            if typ == "ReturnStatement":
                returns.append(n["loc"]["start"]["line"])
            kinds = {"IfStatement": "conditional", "ForStatement": "loop", "ForInStatement": "loop",
                     "ForOfStatement": "loop", "WhileStatement": "loop", "DoWhileStatement": "loop",
                     "SwitchStatement": "selection", "TryStatement": "exception handling", "ThrowStatement": "raise", "AwaitExpression": "await"}
            if typ in kinds:
                features.append({"kind": kinds[typ], "line": n["loc"]["start"]["line"]})
            for key, value in n.items():
                if key not in ("loc", "range") and isinstance(value, (list, dict)):
                    walk(value)
    walk(body.get("body"))
    return {"parameters": names, "parameters_truncated": len(params) > 30, "parameter_detail": "named_or_pattern",
            "async": bool(body.get("async")), "return_lines": returns[:30], "returns_truncated": len(returns) > 30,
            "return_detail": "explicit_only_arrow_expressions_excluded", "features": features[:30], "features_truncated": len(features) > 30,
            "evidence_lines": evidence_lines(lines, [p["line"] for p in names] + returns[:30] + [f["line"] for f in features[:30]])}


class _Visitor:
    def __init__(self, path, text, digest):
        self.path, self.text, self.lines, self.digest = path, text, text.splitlines(), digest
        self.file_id = "file:" + path
        self.nodes, self.edges, self.calls, self.imports = [], [], [], []
        self.top_level, self.exports, self.pending_exports, self.names, self.stack = {}, {}, [], {}, []
        self.truncated = False

    def line(self, n):
        return self.lines[n - 1][:1000] if 0 < n <= len(self.lines) else ""

    def visit(self, node):
        if isinstance(node, list):
            for item in node:
                self.visit(item)
            return
        if not isinstance(node, dict):
            return
        typ = node.get("type")
        if typ == "ImportDeclaration":
            self.import_declaration(node)
            return
        if typ in ("ExportNamedDeclaration", "ExportDefaultDeclaration"):
            declaration = node.get("declaration")
            before = set(self.top_level)
            if declaration:
                self.visit(declaration)
                added = set(self.top_level) - before
                for name in added:
                    self.pending_exports.append(("default" if typ == "ExportDefaultDeclaration" else name, name))
            if not node.get("source"):
                for item in node.get("specifiers", []):
                    self.pending_exports.append((item["exported"]["name"], item["local"]["name"]))
            return
        if typ == "FunctionDeclaration" and node.get("id"):
            self.function(node["id"]["name"], node)
            return
        if typ == "VariableDeclarator" and node.get("id", {}).get("type") == "Identifier" and (node.get("init") or {}).get("type") in ("ArrowFunctionExpression", "FunctionExpression"):
            self.function(node["id"]["name"], node["init"], node)
            return
        if typ == "CallExpression":
            self.call(node)
        for key, value in node.items():
            if key not in ("loc", "range") and isinstance(value, (dict, list)):
                self.visit(value)

    def function(self, name, body, source_node=None):
        if len(self.nodes) >= 600:
            self.truncated = True
            return
        node = source_node or body
        qual = ".".join([x[0] for x in self.stack] + [name])
        key = "function:" + self.path + ":" + qual
        self.names[key] = self.names.get(key, 0) + 1
        if self.names[key] > 1:
            key += "#" + str(self.names[key])
        start, end = node["loc"]["start"]["line"], node["loc"]["end"]["line"]
        start_offset, end_offset = node["range"]
        fingerprint = hashlib.sha256(self.text[start_offset:end_offset].encode()).hexdigest()
        snippet = "\n".join(self.line(i) for i in range(start, min(end, start + 11) + 1))
        parent = self.stack[-1][1] if self.stack else self.file_id
        self.nodes.append({"id": key, "kind": "function", "name": qual, "path": self.path,
                           "line": start, "end_line": end, "snippet": snippet, "sha256": self.digest,
                           "fingerprint": fingerprint, "language": "javascript", "shape": _shape(body, self.lines), "evidence": "static"})
        self.edges.append({"source": parent, "target": key, "kind": "defines", "evidence": "static", "path": self.path, "line": start})
        if not self.stack:
            self.top_level.setdefault(name, []).append(key)
        self.stack.append((name, key))
        self.visit(body.get("body"))
        self.stack.pop()

    def call(self, node):
        callee = node.get("callee") or {}
        base, attribute = None, None
        if callee.get("type") == "Identifier":
            base = callee.get("name")
        elif (callee.get("type") == "MemberExpression" and not callee.get("computed") and
              callee.get("object", {}).get("type") == "Identifier" and callee.get("property", {}).get("type") == "Identifier"):
            base, attribute = callee["object"]["name"], callee["property"]["name"]
        if len(self.calls) >= 3000:
            self.truncated = True
            return
        line = node["loc"]["start"]["line"]
        start, end = callee.get("range", (0, 0))
        raw_written = self.text[start:end] or "[complex call]"
        written = raw_written[:200]
        self.calls.append({"caller": self.stack[-1][1] if self.stack else self.file_id,
                           "base": base, "attribute": attribute, "written": written, "written_truncated": len(raw_written) > 200,
                           "written_arguments": len(node.get("arguments") or []), "line": line, "snippet": self.line(line),
                           "snippet_truncated": len(self.lines[line - 1]) > 1000,
                           "line_sha256": hashlib.sha256(self.lines[line - 1].encode("utf-8")).hexdigest()})

    def import_declaration(self, node):
        source, line = node.get("source", {}).get("value"), node["loc"]["start"]["line"]
        if not isinstance(source, str) or len(self.nodes) >= 600:
            self.truncated = True
            return
        key = f"import:{self.path}:{line}:{source}"
        self.names[key] = self.names.get(key, 0) + 1
        if self.names[key] > 1:
            key += "#" + str(self.names[key])
        self.nodes.append({"id": key, "kind": "dependency", "name": source, "path": self.path,
                           "line": line, "sha256": self.digest, "language": "javascript", "evidence": "static"})
        self.edges.append({"source": self.file_id, "target": key, "kind": "imports", "evidence": "static", "path": self.path, "line": line})
        specifiers = node.get("specifiers") or [None]
        for spec in specifiers:
            typ = spec.get("type") if spec else None
            symbol = (spec.get("imported") or {}).get("name") if typ == "ImportSpecifier" else "default" if typ == "ImportDefaultSpecifier" else None
            self.imports.append({"id": key, "source": source, "bound": (spec.get("local") or {}).get("name") if spec else None,
                                 "symbol": symbol, "namespace": typ == "ImportNamespaceSpecifier", "line": line, "snippet": self.line(line)})

    def finish_exports(self):
        for exported, local in self.pending_exports:
            matches = self.top_level.get(local, [])
            if len(matches) == 1:
                self.exports.setdefault(exported, []).append(matches[0])

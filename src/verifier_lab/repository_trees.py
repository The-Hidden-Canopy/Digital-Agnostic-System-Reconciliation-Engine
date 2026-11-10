"""Bounded tree-sitter projections for TypeScript, TSX/JSX and C++ source."""
from __future__ import annotations

import hashlib


def parse_source(data, path, language):
    try:
        from tree_sitter import Language, Parser
        if language == "cpp":
            import tree_sitter_cpp as grammar
            capsule = grammar.language()
        else:
            import tree_sitter_typescript as grammar
            capsule = grammar.language_tsx() if language in ("tsx", "jsx") else grammar.language_typescript()
    except ImportError:
        return {"error": "language parser unavailable; install the languages extra"}
    try:
        data.decode("utf-8-sig")
        root = Parser(Language(capsule)).parse(data).root_node
        visitor = _TreeVisitor(data, path, language)
        visitor.visit(root)
        visitor.finish()
        return {"nodes": visitor.nodes, "edges": visitor.edges, "imports": visitor.imports,
                "calls": visitor.calls, "top_level": visitor.top_level, "exports": visitor.exports,
                "truncated": visitor.truncated, "partial": root.has_error}
    except (UnicodeError, ValueError, RecursionError, MemoryError):
        return {"error": "invalid or overly complex source"}


class _TreeVisitor:
    def __init__(self, data, path, language):
        self.data, self.path, self.language = data, path, language
        self.digest = hashlib.sha256(data).hexdigest()
        self.lines = data.decode("utf-8-sig").splitlines()
        self.file_id = "file:" + path
        self.nodes, self.edges, self.imports, self.calls = [], [], [], []
        self.top_level, self.exports, self.pending_exports, self.names = {}, {}, [], {}
        self.stack, self.scopes = [], []
        self.class_depth = 0
        self.truncated = False

    def text(self, node):
        return self.data[node.start_byte:node.end_byte].decode("utf-8", errors="replace") if node else ""

    def line(self, node):
        return node.start_point.row + 1

    def snippet(self, node):
        n = self.line(node)
        return self.lines[n - 1][:1000] if n <= len(self.lines) else ""

    def visit(self, node):
        typ = node.type
        if typ == "ERROR" or node.is_missing:
            return
        if node.has_error and typ in ("function_definition", "function_declaration", "generator_function_declaration",
                                      "variable_declarator", "import_statement", "export_statement", "preproc_include"):
            return
        if self.language == "cpp":
            if typ == "preproc_include":
                self.include(node)
                return
            if typ in ("namespace_definition", "class_specifier", "struct_specifier"):
                name = self.text(node.child_by_field_name("name"))
                is_class = typ in ("class_specifier", "struct_specifier")
                if name:
                    self.scopes.append(name)
                if is_class:
                    self.class_depth += 1
                for child in node.named_children:
                    if child.type not in ("namespace_identifier", "type_identifier"):
                        self.visit(child)
                if is_class:
                    self.class_depth -= 1
                if name:
                    self.scopes.pop()
                return
            if typ == "function_definition":
                dec = node.child_by_field_name("declarator")
                while dec and dec.type != "function_declarator":
                    dec = dec.child_by_field_name("declarator")
                name = self.text(dec.child_by_field_name("declarator")) if dec else ""
                if name:
                    self.function(name, node, node.child_by_field_name("body"))
                    return
        else:
            if typ == "import_statement":
                self.import_ts(node)
                return
            if typ == "export_statement":
                if node.child_by_field_name("source"):
                    return
                before = set(self.top_level)
                for child in node.named_children:
                    if child.type == "export_clause":
                        for spec in child.named_children:
                            if spec.type == "export_specifier":
                                name = self.text(spec.child_by_field_name("name"))
                                alias = self.text(spec.child_by_field_name("alias")) or name
                                self.pending_exports.append((alias, name))
                    else:
                        self.visit(child)
                default = self.text(node).lstrip().startswith("export default")
                for name in set(self.top_level) - before:
                    self.pending_exports.append(("default" if default else name, name))
                return
            if typ in ("function_declaration", "generator_function_declaration"):
                name = self.text(node.child_by_field_name("name"))
                if name and node.child_by_field_name("body"):
                    self.function(name, node, node.child_by_field_name("body"))
                    return
            if typ == "variable_declarator":
                name, value = node.child_by_field_name("name"), node.child_by_field_name("value")
                if name and name.type == "identifier" and value and value.type in ("arrow_function", "function_expression"):
                    self.function(self.text(name), node, value.child_by_field_name("body"))
                    return
        if typ == "call_expression":
            self.call(node)
        for child in node.named_children:
            self.visit(child)

    def shape(self, node, body):
        from .repository_learning import evidence_lines
        signature = node.child_by_field_name("value") if node.type == "variable_declarator" else node
        if self.language == "cpp":
            signature = node.child_by_field_name("declarator")
            while signature and signature.type != "function_declarator":
                signature = signature.child_by_field_name("declarator")
        parameters = signature.child_by_field_name("parameters") if signature else None
        if parameters is None and signature:
            parameters = signature.child_by_field_name("parameter")
        items = parameters.named_children if parameters and parameters.type in ("formal_parameters", "parameter_list") else ([parameters] if parameters else [])
        entries = []
        for item in items[:30]:
            identifier = item if item.type == "identifier" else item.child_by_field_name("pattern") or item.child_by_field_name("declarator")
            name = self.text(identifier) if identifier and identifier.type == "identifier" else "[pattern or unnamed]"
            entries.append({"name": name, "kind": "named" if name != "[pattern or unnamed]" else "pattern", "line": self.line(item)})
        returns, features = [], []
        def walk(current):
            if current.type in ("function_definition", "function_declaration", "function_expression", "arrow_function", "lambda_expression", "class_declaration"):
                return
            if current.type == "return_statement":
                returns.append(self.line(current))
            kinds = {"if_statement": "conditional", "for_statement": "loop", "for_in_statement": "loop",
                     "for_range_loop": "loop", "while_statement": "loop", "do_statement": "loop",
                     "switch_statement": "selection", "try_statement": "exception handling", "throw_statement": "raise",
                     "await_expression": "await", "co_await_expression": "await"}
            if current.type in kinds:
                features.append({"kind": kinds[current.type], "line": self.line(current)})
            for child in current.named_children:
                walk(child)
        if body:
            walk(body)
        is_async = None if self.language == "cpp" else bool(signature and any(child.type == "async" for child in signature.children))
        return {"parameters": entries, "parameters_truncated": len(items) > 30, "parameter_detail": "named_or_pattern",
                "async": is_async, "return_lines": returns[:30], "returns_truncated": len(returns) > 30,
                "return_detail": "explicit_only_arrow_expressions_excluded" if self.language != "cpp" else "explicit",
                "features": features[:30], "features_truncated": len(features) > 30,
                "evidence_lines": evidence_lines(self.lines, [p["line"] for p in entries] + returns[:30] + [f["line"] for f in features[:30]])}

    def function(self, name, node, body):
        if len(self.nodes) >= 600:
            self.truncated = True
            return
        qual = ".".join(self.scopes + [x[0] for x in self.stack] + [name])
        key = "function:" + self.path + ":" + qual
        self.names[key] = self.names.get(key, 0) + 1
        if self.names[key] > 1:
            key += "#" + str(self.names[key])
        start, end = self.line(node), node.end_point.row + 1
        snippet = "\n".join(self.lines[i - 1][:1000] for i in range(start, min(end, start + 11) + 1))
        parent = self.stack[-1][1] if self.stack else self.file_id
        self.nodes.append({"id": key, "kind": "function", "name": qual, "path": self.path,
                           "line": start, "end_line": end, "snippet": snippet, "sha256": self.digest,
                           "fingerprint": hashlib.sha256(self.data[node.start_byte:node.end_byte]).hexdigest(),
                           "language": self.language, "shape": self.shape(node, body), "evidence": "static"})
        self.edges.append({"source": parent, "target": key, "kind": "defines", "evidence": "static", "path": self.path, "line": start})
        # Bare-name matches remain inference, and ambiguities are not resolved.
        if not self.stack and not self.class_depth and (self.language != "cpp" or "::" not in name):
            short = name.rsplit("::", 1)[-1]
            self.top_level.setdefault(short, []).append(key)
        self.stack.append((name, key))
        if body:
            self.visit(body)
        self.stack.pop()

    def call(self, node):
        if len(self.calls) >= 3000:
            self.truncated = True
            return
        func = node.child_by_field_name("function")
        if not func:
            return
        base, attr = None, None
        if func.type == "identifier":
            base = self.text(func)
        elif self.language != "cpp" and func.type == "member_expression":
            obj, prop = func.child_by_field_name("object"), func.child_by_field_name("property")
            if obj and obj.type == "identifier" and prop and prop.type == "property_identifier":
                base, attr = self.text(obj), self.text(prop)
        args = node.child_by_field_name("arguments")
        count = len([item for item in args.named_children if item.type != "comment"]) if args else None
        raw_written = self.text(func) or "[complex call]"
        line = self.line(node)
        self.calls.append({"caller": self.stack[-1][1] if self.stack else self.file_id, "base": base,
                           "attribute": attr, "written": raw_written[:200], "written_truncated": len(raw_written) > 200,
                           "written_arguments": count, "line": line, "snippet": self.snippet(node),
                           "snippet_truncated": line <= len(self.lines) and len(self.lines[line - 1]) > 1000,
                           "line_sha256": hashlib.sha256((self.lines[line - 1] if line <= len(self.lines) else "").encode("utf-8")).hexdigest()})

    def add_dependency(self, node, source):
        if len(self.nodes) >= 600:
            self.truncated = True
            return None
        line = self.line(node)
        key = f"import:{self.path}:{line}:{source}"
        self.names[key] = self.names.get(key, 0) + 1
        if self.names[key] > 1:
            key += "#" + str(self.names[key])
        self.nodes.append({"id": key, "kind": "dependency", "name": source, "path": self.path,
                           "line": line, "sha256": self.digest, "language": self.language, "evidence": "static"})
        self.edges.append({"source": self.file_id, "target": key, "kind": "includes" if self.language == "cpp" else "imports", "evidence": "static", "path": self.path, "line": line})
        return key

    def import_ts(self, node):
        source_node = node.child_by_field_name("source")
        source = self.text(source_node)
        if len(source) < 2 or source[0] not in "\"'" or source[-1] != source[0] or "\\" in source:
            return
        source = source[1:-1]
        key = self.add_dependency(node, source)
        if not key:
            return
        records = []
        type_only = self.text(node).lstrip().startswith("import type ")
        for clause in node.named_children:
            if clause.type != "import_clause":
                continue
            for part in clause.named_children:
                if part.type == "identifier":
                    records.append((self.text(part), "default", False))
                elif part.type == "namespace_import":
                    ids = [c for c in part.named_children if c.type == "identifier"]
                    if ids:
                        records.append((self.text(ids[-1]), None, True))
                elif part.type == "named_imports":
                    for spec in part.named_children:
                        if spec.type != "import_specifier" or self.text(spec).lstrip().startswith("type "):
                            continue
                        name = self.text(spec.child_by_field_name("name"))
                        alias = self.text(spec.child_by_field_name("alias")) or name
                        records.append((alias, name, False))
        if type_only:
            records = []
        for bound, symbol, namespace in records or [(None, None, False)]:
            self.imports.append({"id": key, "source": source, "bound": bound, "symbol": symbol,
                                 "namespace": namespace, "line": self.line(node), "snippet": self.snippet(node)})

    def include(self, node):
        path = node.child_by_field_name("path")
        if not path:
            return
        raw = self.text(path)
        if len(raw) < 2 or raw[0] not in ('"', '<') or raw[-1] not in ('"', '>'):
            return
        source = raw[1:-1]
        key = self.add_dependency(node, raw)
        if key:
            self.imports.append({"id": key, "source": source, "local": raw[0] == '"',
                                 "line": self.line(node), "snippet": self.snippet(node)})

    def finish(self):
        for exported, local in self.pending_exports:
            matches = self.top_level.get(local, [])
            if len(matches) == 1:
                self.exports.setdefault(exported, []).append(matches[0])

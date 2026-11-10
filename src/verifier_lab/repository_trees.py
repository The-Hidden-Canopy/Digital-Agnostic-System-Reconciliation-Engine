"""Bounded tree-sitter projections for TypeScript, TSX/JSX, JavaScript, C, C++, C# and Rust source.

Every record is written syntax. Nothing here resolves a call target; the scanner infers
candidates afterwards and labels them as inferred.
"""
from __future__ import annotations

import hashlib

# language -> (grammar module, factory attribute, syntax family)
GRAMMARS = {
    "typescript": ("tree_sitter_typescript", "language_typescript", "ts"),
    "tsx": ("tree_sitter_typescript", "language_tsx", "ts"),
    "jsx": ("tree_sitter_typescript", "language_tsx", "ts"),
    "javascript": ("tree_sitter_typescript", "language_tsx", "ts"),
    "cpp": ("tree_sitter_cpp", "language", "c"),
    "c": ("tree_sitter_c", "language", "c"),
    "csharp": ("tree_sitter_c_sharp", "language", "csharp"),
    "rust": ("tree_sitter_rust", "language", "rust"),
}
MAX_FILE_NODES, MAX_FILE_CALLS = 4000, 20000
_PARSERS = {}

_CLASS_TYPES = {
    "ts": ("class_declaration", "abstract_class_declaration", "class"),
    "c": ("class_specifier", "struct_specifier"),
    "csharp": ("class_declaration", "struct_declaration", "interface_declaration", "record_declaration",
               "record_struct_declaration"),
    "rust": ("impl_item", "trait_item"),
}
_NESTED_BODIES = {"function_definition", "function_declaration", "function_expression", "arrow_function",
                  "lambda_expression", "class_declaration", "method_definition", "function_item",
                  "closure_expression", "local_function_statement", "anonymous_method_expression",
                  "method_declaration", "generator_function_declaration", "generator_function"}
_FEATURES = {"if_statement": "conditional", "for_statement": "loop", "for_in_statement": "loop",
             "for_range_loop": "loop", "while_statement": "loop", "do_statement": "loop",
             "switch_statement": "selection", "try_statement": "exception handling", "throw_statement": "raise",
             "await_expression": "await", "co_await_expression": "await",
             # C#
             "foreach_statement": "loop", "switch_expression": "selection", "throw_expression": "raise",
             # Rust
             "if_expression": "conditional", "for_expression": "loop", "while_expression": "loop",
             "loop_expression": "loop", "match_expression": "pattern match"}
_RETURNS = ("return_statement", "return_expression")
_RETURN_DETAIL = {"ts": "explicit_only_arrow_expressions_excluded", "c": "explicit",
                  "csharp": "explicit_only_expression_bodies_excluded", "rust": "explicit_only_tail_expressions_excluded"}


def _parser(language):
    if language not in _PARSERS:
        import importlib
        from tree_sitter import Language, Parser
        module, factory, _ = GRAMMARS[language]
        _PARSERS[language] = Parser(Language(getattr(importlib.import_module(module), factory)()))
    return _PARSERS[language]


def parse_source(data, path, language):
    if language not in GRAMMARS:
        return {"error": "unsupported language"}
    try:
        parser = _parser(language)
    except ImportError:
        return {"error": "language parser unavailable; install the languages extra"}
    try:
        data.decode("utf-8-sig")
        root = parser.parse(data).root_node
        visitor = _TreeVisitor(data, path, language)
        visitor.visit(root)
        visitor.finish()
        return {"nodes": visitor.nodes, "edges": visitor.edges, "imports": visitor.imports,
                "calls": visitor.calls, "top_level": visitor.top_level, "exports": visitor.exports,
                "members": visitor.members, "qualified": visitor.qualified,
                "truncated": visitor.truncated, "partial": root.has_error}
    except (UnicodeError, ValueError, RecursionError, MemoryError):
        return {"error": "invalid or overly complex source"}


class _TreeVisitor:
    def __init__(self, data, path, language):
        self.data, self.path, self.language = data, path, language
        self.family = GRAMMARS[language][2]
        self.digest = hashlib.sha256(data).hexdigest()
        self.lines = data.decode("utf-8-sig").splitlines()
        self.file_id = "file:" + path
        self.nodes, self.edges, self.imports, self.calls = [], [], [], []
        self.top_level, self.exports, self.pending_exports, self.names = {}, {}, [], {}
        self.members, self.qualified = {}, {}
        self.stack, self.scopes, self.classes, self.local_scopes = [], [], [], []
        self.class_depth = self.anonymous_depth = 0
        self.truncated = False

    def text(self, node):
        return self.data[node.start_byte:node.end_byte].decode("utf-8", errors="replace") if node else ""

    def line(self, node):
        return node.start_point.row + 1

    def snippet(self, node):
        n = self.line(node)
        return self.lines[n - 1][:1000] if n <= len(self.lines) else ""

    # ------------------------------------------------------------------ traversal
    def visit(self, node):
        typ = node.type
        if typ == "ERROR" or node.is_missing:
            return
        if node.has_error and typ in ("function_definition", "function_declaration", "generator_function_declaration",
                                      "variable_declarator", "import_statement", "export_statement", "preproc_include",
                                      "method_definition", "method_declaration", "constructor_declaration",
                                      "local_function_statement", "function_item", "use_declaration", "using_directive"):
            return
        handler = getattr(self, "visit_" + self.family)
        if handler(node, typ):
            return
        if typ in ("call_expression", "invocation_expression"):
            self.call(node)
        for child in node.named_children:
            self.visit(child)

    def scoped(self, node, name, is_class, skip=()):
        """Visit a namespace or type body with its name on the scope stack."""
        if name:
            self.scopes.append(name)
        if is_class:
            self.class_depth += 1
            self.classes.append(".".join(self.scopes))
        for child in node.named_children:
            if child.type not in skip:
                self.visit(child)
        if is_class:
            self.classes.pop()
            self.class_depth -= 1
        if name:
            self.scopes.pop()

    def visit_c(self, node, typ):
        if typ == "preproc_include":
            self.include(node)
            return True
        if typ in ("namespace_definition", "class_specifier", "struct_specifier"):
            name = self.text(node.child_by_field_name("name"))
            unnamed = typ == "namespace_definition" and not name  # namespace { ... }: visible in this file only
            self.anonymous_depth += unnamed
            self.scoped(node, name, typ != "namespace_definition", skip=("namespace_identifier", "type_identifier"))
            self.anonymous_depth -= unnamed
            return True
        if typ == "function_definition":
            dec = node.child_by_field_name("declarator")
            while dec and dec.type != "function_declarator":
                dec = dec.child_by_field_name("declarator")
            name = self.text(dec.child_by_field_name("declarator")) if dec else ""
            if name:
                self.function(name, node, node.child_by_field_name("body"))
                return True
        return False

    def visit_ts(self, node, typ):
        if typ == "import_statement":
            self.import_ts(node)
            return True
        if typ == "export_statement":
            if node.child_by_field_name("source"):
                return True
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
            return True
        if typ in ("function_declaration", "generator_function_declaration"):
            name = self.text(node.child_by_field_name("name"))
            if name and node.child_by_field_name("body"):
                self.function(name, node, node.child_by_field_name("body"))
                return True
        if typ == "variable_declarator":
            name, value = node.child_by_field_name("name"), node.child_by_field_name("value")
            if name and name.type == "identifier" and value and value.type in ("arrow_function", "function_expression"):
                self.function(self.text(name), node, value.child_by_field_name("body"))
                return True
            if value and value.type == "call_expression" and self.text(value.child_by_field_name("function")) == "require":
                self.require_ts(node, name, value)
        if typ in ("class_declaration", "abstract_class_declaration"):
            name = self.text(node.child_by_field_name("name"))
            if name:
                self.scoped(node, name, True, skip=("type_identifier", "identifier"))
                return True
        if typ == "method_definition":
            name_node, body = node.child_by_field_name("name"), node.child_by_field_name("body")
            if name_node and name_node.type == "property_identifier" and body and self.class_depth:
                self.function(self.text(name_node), node, body)
                return True
        if typ == "assignment_expression":
            self.commonjs_export(node)
        return False

    def visit_csharp(self, node, typ):
        if typ == "using_directive":
            name = " ".join(self.text(node).rstrip(";").split()[1:])
            if name:
                key = self.add_dependency(node, name)
                if key:
                    self.imports.append({"id": key, "source": name, "bound": None, "symbol": None, "namespace": False,
                                         "line": self.line(node), "snippet": self.snippet(node)})
            return True
        if typ == "file_scoped_namespace_declaration":
            name = self.text(node.child_by_field_name("name"))
            if name:
                self.scopes.append(name)  # applies to the remainder of the file
            return True
        if typ == "namespace_declaration":
            self.scoped(node, self.text(node.child_by_field_name("name")), False, skip=("identifier", "qualified_name"))
            return True
        if typ in _CLASS_TYPES["csharp"]:
            name = self.text(node.child_by_field_name("name"))
            if name:
                self.scoped(node, name, True, skip=("identifier",))
                return True
        if typ in ("method_declaration", "constructor_declaration", "local_function_statement"):
            name, body = self.text(node.child_by_field_name("name")), node.child_by_field_name("body")
            if name and body:
                self.function(name, node, body)
                return True
        return False

    def visit_rust(self, node, typ):
        if typ == "use_declaration":
            self.use_rust(node)
            return True
        if typ == "mod_item":
            name, body = self.text(node.child_by_field_name("name")), node.child_by_field_name("body")
            if not body:
                key = self.add_dependency(node, "mod " + name) if name else None
                if key:
                    self.imports.append({"id": key, "source": "mod " + name, "segments": ["self", name], "bound": name,
                                         "module_declaration": True, "line": self.line(node), "snippet": self.snippet(node)})
                return True
            self.scoped(body, name, False)
            return True
        if typ in ("impl_item", "trait_item"):
            owner = node.child_by_field_name("type") if typ == "impl_item" else node.child_by_field_name("name")
            name = self.text(owner).split("<", 1)[0].strip()
            body = node.child_by_field_name("body")
            if name and body:
                self.scoped(body, name, True)
                return True
        if typ == "function_item":
            name, body = self.text(node.child_by_field_name("name")), node.child_by_field_name("body")
            if name and body:
                self.function(name, node, body)
                return True
        return False

    # ------------------------------------------------------------------ shapes
    def _parameters(self, node):
        signature = node.child_by_field_name("value") if node.type == "variable_declarator" else node
        if self.family == "c":
            signature = node.child_by_field_name("declarator")
            while signature and signature.type != "function_declarator":
                signature = signature.child_by_field_name("declarator")
        parameters = signature.child_by_field_name("parameters") if signature else None
        if parameters is None and signature:
            parameters = signature.child_by_field_name("parameter")
        items = parameters.named_children if parameters and parameters.type in ("formal_parameters", "parameter_list", "parameters") else ([parameters] if parameters else [])
        return signature, [item for item in items if item.type not in ("comment", "line_comment", "block_comment", "attribute_item")]

    def _parameter_name(self, item):
        if item.type == "identifier":
            return self.text(item)
        if item.type == "self_parameter":
            return "self"
        identifier = item.child_by_field_name("pattern") or item.child_by_field_name("declarator") or item.child_by_field_name("name")
        if self.family == "c":
            # int *values, const T& item and int grid[4] still name a parameter.
            while identifier is not None and identifier.type in ("pointer_declarator", "reference_declarator", "array_declarator"):
                inner = identifier.child_by_field_name("declarator")
                if inner is None:
                    inner = next((child for child in identifier.named_children if child.type == "identifier"), None)
                identifier = inner
        return self.text(identifier) if identifier is not None and identifier.type == "identifier" else None

    def _arity(self, signature, items):
        """How many written arguments the parameter list can accept, where the syntax settles it."""
        if signature is None or self.language in ("javascript", "jsx"):
            return None  # JavaScript accepts any number of arguments
        required = optional = 0
        variadic = takes_self = False
        for item in items:
            text = self.text(item)
            if item.type == "self_parameter":
                takes_self = True
            elif item.type in ("variadic_parameter_declaration", "variadic_parameter", "rest_pattern") or text.strip() == "...":
                variadic = True
            elif self.family == "c":
                if item.type == "parameter_declaration" and text.strip() == "void":
                    continue
                optional += item.type == "optional_parameter_declaration"
                required += item.type != "optional_parameter_declaration"
            elif self.family == "ts":
                pattern = item.child_by_field_name("pattern")
                if pattern is not None and pattern.type == "rest_pattern":
                    variadic = True
                elif pattern is not None and pattern.type == "this":
                    continue
                elif item.type == "optional_parameter" or item.child_by_field_name("value") is not None:
                    optional += 1
                else:
                    required += 1
            elif self.family == "csharp":
                tokens = [child.type for child in item.children]
                if any(self.text(child) == "params" for child in item.children):
                    variadic = True
                elif "=" in tokens or "equals_value_clause" in tokens:
                    optional += 1
                else:
                    required += 1
            else:
                required += 1
        if self.language == "c" and not items:
            variadic = True  # f() in C leaves the parameters unspecified
        maximum = None if variadic else required + optional
        if self.language == "cpp":
            required = 0  # default arguments are usually written on a declaration in another file
        return {"min": required, "max": maximum, "self": takes_self}

    def _declared(self, node, body):
        """(names bound to values, names of functions declared) directly in this function, nested bodies excluded."""
        values, functions = set(), set()
        _, items = self._parameters(node)
        for item in items:
            name = self._parameter_name(item)
            if name:
                values.add(name)
            elif item.type not in ("self_parameter",):
                values.update(self._identifiers(item.child_by_field_name("pattern") or item))

        def declarator_name(current):
            while current is not None and current.type in ("init_declarator", "pointer_declarator", "reference_declarator", "array_declarator", "parenthesized_declarator"):
                inner = current.child_by_field_name("declarator")
                current = inner if inner is not None else next((c for c in current.named_children if c.type in ("identifier", "structured_binding_declarator")), None)
            if current is None or current.type == "function_declarator":
                return []  # a local prototype names a function defined elsewhere
            return self._identifiers(current) if current.type == "structured_binding_declarator" else ([self.text(current)] if current.type == "identifier" else [])

        def walk(current):
            typ = current.type
            if typ in ("function_declaration", "generator_function_declaration", "function_item", "local_function_statement"):
                functions.add(self.text(current.child_by_field_name("name")))
                return
            if typ in _NESTED_BODIES:
                return
            if self.family == "c":
                if typ in ("declaration", "for_range_loop", "condition_declaration", "parameter_declaration"):
                    for index, child in enumerate(current.children):
                        if current.field_name_for_child(index) == "declarator":
                            values.update(declarator_name(child))
            elif self.family == "ts":
                if typ == "variable_declarator":
                    name, value = current.child_by_field_name("name"), current.child_by_field_name("value")
                    if name is not None and name.type == "identifier" and value is not None and value.type in ("arrow_function", "function_expression"):
                        functions.add(self.text(name))
                    elif name is not None:
                        values.update(self._identifiers(name))
                elif typ == "catch_clause" and current.child_by_field_name("parameter") is not None:
                    values.update(self._identifiers(current.child_by_field_name("parameter")))
            elif self.family == "csharp":
                if typ in ("variable_declarator", "declaration_expression", "catch_declaration", "single_variable_designation"):
                    name = current.child_by_field_name("name")
                    values.update([self.text(name)] if name is not None else self._identifiers(current)[:1])
                elif typ == "foreach_statement" and current.child_by_field_name("left") is not None:
                    values.update(self._identifiers(current.child_by_field_name("left")))
            elif self.family == "rust":
                if typ in ("let_declaration", "for_expression", "let_condition", "match_arm") and current.child_by_field_name("pattern") is not None:
                    values.update(self._identifiers(current.child_by_field_name("pattern")))
            for child in current.named_children:
                walk(child)
        if body is not None:
            walk(body)
        functions.discard("")
        return values - functions, functions

    def _identifiers(self, node, limit=40):
        found, pending = [], [node] if node is not None else []
        while pending and len(found) < limit:
            current = pending.pop()
            if current.type in ("identifier", "shorthand_property_identifier_pattern"):
                found.append(self.text(current))
            else:
                pending.extend(current.named_children)
        return found

    def shape(self, node, body):
        from .repository_learning import evidence_lines
        signature, items = self._parameters(node)
        entries = []
        for item in items[:30]:
            name = self._parameter_name(item) or "[pattern or unnamed]"
            entries.append({"name": name, "kind": "named" if name != "[pattern or unnamed]" else "pattern", "line": self.line(item)})
        returns, features = [], []

        def walk(current):
            if current.type in _NESTED_BODIES:
                return
            if current.type in _RETURNS:
                returns.append(self.line(current))
            if current.type in _FEATURES:
                features.append({"kind": _FEATURES[current.type], "line": self.line(current)})
            for child in current.named_children:
                walk(child)
        if body:
            walk(body)
        if self.family == "c":
            is_async = None
        elif self.family == "ts":
            is_async = bool(signature and any(child.type == "async" for child in signature.children))
        else:
            head = self.data[node.start_byte:(body.start_byte if body else node.end_byte)].decode("utf-8", errors="replace")
            is_async = "async" in head.replace("(", " ").split()
        return {"parameters": entries, "parameters_truncated": len(items) > 30, "parameter_detail": "named_or_pattern",
                "async": is_async, "return_lines": returns[:30], "returns_truncated": len(returns) > 30,
                "return_detail": _RETURN_DETAIL[self.family], "arity": self._arity(signature, items),
                "features": features[:30], "features_truncated": len(features) > 30,
                "evidence_lines": evidence_lines(self.lines, [p["line"] for p in entries] + returns[:30] + [f["line"] for f in features[:30]])}

    # ------------------------------------------------------------------ records
    def function(self, name, node, body):
        if len(self.nodes) >= MAX_FILE_NODES:
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
        if self.family == "c" and not self.class_depth and (self.anonymous_depth or any(
                child.type == "storage_class_specifier" and self.text(child) == "static" for child in node.children)):
            self.nodes[-1]["file_local"] = True  # static or in an unnamed namespace: not callable from other files
        self.edges.append({"source": parent, "target": key, "kind": "defines", "evidence": "static", "path": self.path, "line": start})
        owner = None
        if not self.stack:
            short = name.rsplit("::", 1)[-1]
            if self.class_depth:
                owner = self.classes[-1] or None  # an unnamed type has no member index
            elif self.family == "c" and "::" in name:
                # void Widget::draw() defined outside the class body.
                owner = ".".join(self.scopes + name.split("::")[:-1])
            else:
                # Bare-name matches remain inference, and ambiguities are not resolved.
                self.top_level.setdefault(short, []).append(key)
                if self.scopes:
                    self.qualified.setdefault(".".join(self.scopes + [short]), []).append(key)
            if owner:
                self.members.setdefault(owner, {}).setdefault(short, []).append(key)
        self.stack.append((name, key, owner))
        values, functions = self._declared(node, body)
        self.local_scopes.append((values, functions, self.scopes + [x[0] for x in self.stack]))
        if body:
            self.visit(body)
        self.local_scopes.pop()
        self.stack.pop()

    def owner(self):
        """The type whose member body is being read, if any."""
        for _, _, owner in reversed(self.stack):
            if owner:
                return owner
        return self.classes[-1] if self.classes else None

    def call(self, node):
        if len(self.calls) >= MAX_FILE_CALLS:
            self.truncated = True
            return
        func = node.child_by_field_name("function")
        if not func:
            return
        base, attr, member, scope = None, None, None, None
        local, local_target = False, None
        owner = self.owner()
        if func.type == "identifier":
            base = self.text(func)
            for values, functions, prefix in reversed(self.local_scopes):
                if base in functions:
                    local_target = "function:" + self.path + ":" + ".".join(prefix + [base])
                    break
                if base in values:
                    local = True  # a parameter, local variable or local lambda of this name is what is called
                    break
            if owner and self.family in ("c", "csharp") and not local and not local_target:
                member = base  # a bare name inside a member body may be an implicit member call
        elif self.family == "ts" and func.type == "member_expression":
            obj, prop = func.child_by_field_name("object"), func.child_by_field_name("property")
            if obj and prop and prop.type == "property_identifier":
                if obj.type == "identifier":
                    base, attr = self.text(obj), self.text(prop)
                elif obj.type == "this" and owner:
                    member = self.text(prop)
        elif self.family == "csharp" and func.type == "member_access_expression":
            obj, prop = func.child_by_field_name("expression"), func.child_by_field_name("name")
            if obj and prop and prop.type == "identifier":
                if obj.type == "identifier":
                    base, attr = self.text(obj), self.text(prop)
                elif obj.type in ("this_expression", "this") and owner:
                    member = self.text(prop)
        elif self.family == "rust" and func.type == "field_expression":
            obj, prop = func.child_by_field_name("value"), func.child_by_field_name("field")
            if obj and prop and prop.type == "field_identifier":
                if obj.type == "self" and owner:
                    member = self.text(prop)
                elif obj.type == "identifier":
                    base, attr = self.text(obj), self.text(prop)
        elif func.type == "qualified_identifier":
            # C++ nests on the name side: a::b::c is (scope a, name (scope b, name c)).
            parts, current = [], func
            while current is not None and current.type == "qualified_identifier" and len(parts) < 12:
                parts.append(self.text(current.child_by_field_name("scope")))
                current = current.child_by_field_name("name")
            if current is not None and current.type == "identifier" and parts and all(part.isidentifier() for part in parts):
                scope, attr = "::".join(parts), self.text(current)
        elif func.type == "scoped_identifier":
            path, name = func.child_by_field_name("path"), func.child_by_field_name("name")
            if path is not None and name is not None and name.type == "identifier":
                written_scope = self.text(path)
                if written_scope == "Self" and owner:
                    member = self.text(name)
                elif written_scope and all(part.isidentifier() for part in written_scope.split("::")):
                    scope, attr = written_scope, self.text(name)
        args = node.child_by_field_name("arguments")
        count = len([item for item in args.named_children if item.type not in ("comment", "line_comment", "block_comment")]) if args else None
        raw_written = self.text(func) or "[complex call]"
        line = self.line(node)
        record = {"caller": self.stack[-1][1] if self.stack else self.file_id, "base": base,
                  "attribute": attr, "written": raw_written[:200], "written_truncated": len(raw_written) > 200,
                  "written_arguments": count, "line": line, "snippet": self.snippet(node),
                  "snippet_truncated": line <= len(self.lines) and len(self.lines[line - 1]) > 1000,
                  "line_sha256": hashlib.sha256((self.lines[line - 1] if line <= len(self.lines) else "").encode("utf-8")).hexdigest()}
        if member and owner:
            record["member"], record["member_of"] = member, owner
        if scope:
            record["scope"] = scope
        if local:
            record["local"] = True
        if local_target:
            record["local_target"] = local_target
        if args is not None and any(item.type in ("spread_element", "parameter_pack_expansion") for item in args.named_children):
            record["expands"] = True  # the number of values passed is not visible in the source
        self.calls.append(record)

    def add_dependency(self, node, source):
        if len(self.nodes) >= MAX_FILE_NODES:
            self.truncated = True
            return None
        line = self.line(node)
        key = f"import:{self.path}:{line}:{source}"
        self.names[key] = self.names.get(key, 0) + 1
        if self.names[key] > 1:
            key += "#" + str(self.names[key])
        self.nodes.append({"id": key, "kind": "dependency", "name": source, "path": self.path,
                           "line": line, "sha256": self.digest, "language": self.language, "evidence": "static"})
        self.edges.append({"source": self.file_id, "target": key, "kind": "includes" if self.family == "c" else "imports", "evidence": "static", "path": self.path, "line": line})
        return key

    # ------------------------------------------------------------------ imports
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

    def require_ts(self, node, name, value):
        """const tools = require("./tools") and const {a, b: c} = require("./tools") at module level."""
        if self.stack or self.class_depth:
            return
        args = value.child_by_field_name("arguments")
        items = [item for item in args.named_children if item.type != "comment"] if args else []
        if len(items) != 1 or items[0].type != "string":
            return
        source = self.text(items[0])
        if len(source) < 2 or source[0] not in "\"'" or source[-1] != source[0] or "\\" in source:
            return
        source = source[1:-1]
        records = []
        if name is not None and name.type == "identifier":
            records.append((self.text(name), None, True))
        elif name is not None and name.type == "object_pattern":
            for item in name.named_children:
                if item.type == "shorthand_property_identifier_pattern":
                    records.append((self.text(item), self.text(item), False))
                elif item.type == "pair_pattern":
                    key_node, bound = item.child_by_field_name("key"), item.child_by_field_name("value")
                    if key_node is not None and bound is not None and bound.type == "identifier" and key_node.type == "property_identifier":
                        records.append((self.text(bound), self.text(key_node), False))
        key = self.add_dependency(node, source)
        if not key:
            return
        for bound, symbol, namespace in records or [(None, None, False)]:
            self.imports.append({"id": key, "source": source, "bound": bound, "symbol": symbol, "namespace": namespace,
                                 "commonjs": True, "line": self.line(node), "snippet": self.snippet(node)})

    def commonjs_export(self, node):
        """module.exports = {a, b: c}, module.exports.a = a and exports.a = a at module level."""
        if self.stack or self.class_depth:
            return
        left, right = node.child_by_field_name("left"), node.child_by_field_name("right")
        target = self.text(left)
        if left is None or right is None:
            return
        if target == "module.exports" and right.type == "object":
            for item in right.named_children:
                if item.type == "shorthand_property_identifier":
                    self.pending_exports.append((self.text(item), self.text(item)))
                elif item.type == "pair":
                    key_node, value = item.child_by_field_name("key"), item.child_by_field_name("value")
                    if key_node is not None and value is not None and key_node.type == "property_identifier" and value.type == "identifier":
                        self.pending_exports.append((self.text(key_node), self.text(value)))
        elif target == "module.exports" and right.type == "identifier":
            self.pending_exports.append(("default", self.text(right)))
        elif target.startswith(("module.exports.", "exports.")) and right.type == "identifier":
            exported = target.rsplit(".", 1)[-1]
            if exported.isidentifier():
                self.pending_exports.append((exported, self.text(right)))

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

    def use_rust(self, node):
        argument = node.child_by_field_name("argument")
        written = " ".join(self.text(argument).split())
        if not argument or not written:
            return
        key = self.add_dependency(node, written[:300])
        if not key:
            return
        records = []

        def segments(item):
            if item is None:
                return []
            if item.type == "scoped_identifier":
                return segments(item.child_by_field_name("path")) + [self.text(item.child_by_field_name("name"))]
            return [self.text(item)]

        def flatten(item, prefix):
            if len(records) >= 60:
                return
            if item.type == "use_as_clause":
                path = prefix + segments(item.child_by_field_name("path"))
                records.append((path, self.text(item.child_by_field_name("alias"))))
            elif item.type == "scoped_use_list":
                inner = prefix + segments(item.child_by_field_name("path"))
                listing = item.child_by_field_name("list")
                for child in (listing.named_children if listing else []):
                    flatten(child, inner)
            elif item.type == "use_list":
                for child in item.named_children:
                    flatten(child, prefix)
            elif item.type == "use_wildcard":
                records.append((prefix + segments(item.named_children[0] if item.named_children else None) + ["*"], None))
            elif item.type == "self":
                if prefix:
                    records.append((prefix, prefix[-1]))
            elif item.type in ("identifier", "scoped_identifier", "crate", "super"):
                path = prefix + segments(item)
                records.append((path, path[-1]))
        flatten(argument, [])
        for path, bound in records or [([], None)]:
            self.imports.append({"id": key, "source": "::".join(path), "segments": path, "bound": bound,
                                 "line": self.line(node), "snippet": self.snippet(node)})

    def finish(self):
        for exported, local in self.pending_exports:
            matches = self.top_level.get(local, [])
            if len(matches) == 1:
                self.exports.setdefault(exported, []).append(matches[0])

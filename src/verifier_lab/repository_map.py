"""Read-only, bounded Python repository map. Repository code is never imported or run."""
from __future__ import annotations

import ast
import hashlib
import json
import os
import posixpath
import stat
from collections import defaultdict
from pathlib import Path, PurePosixPath

from .ingestion.common import Limits
from .ingestion.extract import ExtractionFailure, extract_isolated

MAX_FILES, MAX_NODES, MAX_DIRECTORIES, MAX_FILE_BYTES, MAX_EDGES = 200, 600, 1000, 200_000, 3000
MAX_PREVIEW_CHARS, MAX_PREVIEW_LINES, MAX_PREVIEW_LINE_CHARS = 150_000, 120, 500
MAX_CONTEXT_CHARS, MAX_CONTEXT_LINES_PER_FILE, CONTEXT_RADIUS = 200_000, 200, 2
SKIP = {".git", ".hg", ".svn", "node_modules", "vendor", "venv", ".venv", "__pycache__", "build", "dist"}


def _link(path):
    try:
        s = path.lstat()
        return path.is_symlink() or bool(getattr(s, "st_file_attributes", 0) & 0x400)
    except OSError:
        return True


def _read_source(root: Path, relative: Path) -> bytes:
    """Open beneath the selected root; refuse links in directory components where supported."""
    parts = relative.parts
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise OSError("invalid relative source path")
    file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    if os.open in os.supports_dir_fd and getattr(os, "O_NOFOLLOW", 0) and getattr(os, "O_DIRECTORY", 0):
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        directory_fd = os.open(root.anchor, directory_flags)
        try:
            for part in (*root.parts[1:], *parts[:-1]):
                next_fd = os.open(part, directory_flags, dir_fd=directory_fd)
                os.close(directory_fd)
                directory_fd = next_fd
            fd = os.open(parts[-1], file_flags, dir_fd=directory_fd)
        finally:
            os.close(directory_fd)
    else:
        current = root
        for part in parts:
            current = current / part
            if _link(current):
                raise OSError("link or reparse point is not a source file")
        fd = os.open(current, file_flags)
    with os.fdopen(fd, "rb") as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise OSError("source is not a regular file")
        return source.read(MAX_FILE_BYTES + 1)


def _module(path):
    parts = list(PurePosixPath(path).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _import_module(path, module, level):
    if not level:
        return module
    parts = _module(path).split(".") if _module(path) else []
    if not path.endswith("/__init__.py") and path != "__init__.py":
        parts = parts[:-1]
    if level - 1 > len(parts):
        return None
    prefix = parts[:len(parts) - (level - 1)]
    return ".".join(prefix + ([module] if module else []))


def scan_repository(path: str, *, limits: Limits | None = None) -> dict:
    limits = limits or Limits()
    if path.startswith(("\\\\", "//")):
        raise ValueError("Network paths are not supported")
    candidate = Path(path).expanduser().absolute()
    if os.name == "nt" and candidate.drive:
        import ctypes
        if ctypes.windll.kernel32.GetDriveTypeW(str(candidate.anchor)) == 4:
            raise ValueError("Network drives are not supported")
    root = candidate.resolve(strict=True)
    if os.name == "nt" and root.drive and ctypes.windll.kernel32.GetDriveTypeW(str(root.anchor)) == 4:
        raise ValueError("Network drives are not supported")
    if not root.is_dir():
        raise ValueError("Choose a directory containing the repository")
    repo_id = hashlib.sha256(os.fsencode(str(root))).hexdigest()
    nodes = [{"id": "repo", "kind": "repository", "name": root.name, "path": "", "evidence": "static"}]
    edges, warnings, candidates, files = [], [], [], []
    preview_budget, context_budget = MAX_PREVIEW_CHARS, MAX_CONTEXT_CHARS
    def walk_error(error):
        warnings.append("Could not inspect a directory: " + str(error.filename or "unknown"))
    for count, (directory, dirs, names) in enumerate(os.walk(root, followlinks=False, onerror=walk_error), 1):
        if count > MAX_DIRECTORIES:
            warnings.append(f"Directory limit reached ({MAX_DIRECTORIES}).")
            break
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d not in SKIP and not _link(Path(directory) / d))
        for name in sorted(names):
            p = Path(directory) / name
            if not name.startswith(".") and name.endswith((".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".cpp", ".cc", ".cxx", ".h", ".hh", ".hpp", ".hxx")) and not _link(p):
                candidates.append(p)
                if len(candidates) > MAX_FILES:
                    break
        if len(candidates) > MAX_FILES:
            break
    if len(candidates) > MAX_FILES:
        warnings.append(f"File limit reached: only the first {MAX_FILES} supported source files were inspected.")
    for p in candidates[:MAX_FILES]:
        rel = p.relative_to(root).as_posix()
        if len(nodes) >= MAX_NODES - 1:
            warnings.append(f"Node limit reached ({MAX_NODES}); remaining files were not inspected.")
            break
        try:
            raw = _read_source(root, p.relative_to(root))
            if len(raw) > MAX_FILE_BYTES:
                warnings.append(f"Skipped oversized file: {rel}")
                continue
            language = {".py": "python", ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript",
                        ".ts": "typescript", ".tsx": "tsx", ".jsx": "jsx"}.get(p.suffix, "cpp")
            parsed = extract_isolated("repository-" + language, raw, rel, limits)
        except ExtractionFailure as exc:
            if exc.code == "isolation_unavailable":
                raise
            warnings.append(f"Could not parse {rel}: {exc.code}")
            continue
        except OSError:
            warnings.append(f"Could not read: {rel}")
            continue
        if parsed.get("error"):
            warnings.append(f"Could not parse {rel}: {parsed['error']}")
            continue
        if parsed.get("partial"):
            warnings.append(f"Partial syntax coverage in {rel}: error regions and affected declarations were skipped.")
        fid = "file:" + rel
        digest = hashlib.sha256(raw).hexdigest()
        lines = raw.decode("utf-8-sig").splitlines()
        preview = []
        for number, line in enumerate(lines[:MAX_PREVIEW_LINES], 1):
            if preview_budget <= 0:
                break
            available = min(MAX_PREVIEW_LINE_CHARS, max(0, preview_budget - 1))
            preview.append({"line": number, "text": line[:available], "truncated": len(line) > available})
            preview_budget -= len(preview[-1]["text"]) + 1
        captured = {item["line"] for item in preview}
        evidence_points = [line for node in parsed["nodes"] for line in (node.get("shape") or {}).get("evidence_lines", [])]
        points = sorted({item.get("line") for item in parsed["nodes"] + parsed["calls"] + evidence_points
                         if type(item.get("line")) is int and 1 <= item["line"] <= len(lines)})
        wanted = sorted({line for point in points for line in range(max(1, point - CONTEXT_RADIUS), min(len(lines), point + CONTEXT_RADIUS) + 1)
                         if line not in captured})
        context = []
        for number in wanted[:MAX_CONTEXT_LINES_PER_FILE]:
            if context_budget <= 0:
                break
            text = lines[number - 1]
            available = min(MAX_PREVIEW_LINE_CHARS, max(0, context_budget - 1))
            context.append({"line": number, "text": text[:available], "truncated": len(text) > available})
            context_budget -= len(context[-1]["text"]) + 1
        nodes.append({"id": fid, "kind": "file", "name": rel, "path": rel, "sha256": digest,
                      "fingerprint": digest, "language": language, "evidence": "static", "source_preview": preview,
                      "source_preview_truncated": len(preview) < len(lines) or any(line["truncated"] for line in preview),
                      "source_context": context, "source_context_truncated": len(context) < len(wanted) or any(line["truncated"] for line in context)})
        edges.append({"source": "repo", "target": fid, "kind": "contains", "evidence": "static"})
        selected = parsed["nodes"][:MAX_NODES - len(nodes)]
        if parsed.get("truncated") or len(selected) != len(parsed["nodes"]):
            warnings.append(f"Node or call limit reached in {rel}.")
        ids = {n["id"] for n in selected} | {fid}
        nodes.extend(selected)
        edges.extend(e for e in parsed["edges"] if e["source"] in ids and e["target"] in ids)
        files.append({"path": rel, "fid": fid, "parsed": parsed, "ids": ids, "digest": digest, "language": language})
    node_by_id = {node["id"]: node for node in nodes}
    for node in nodes:
        if node["kind"] in ("function", "file"):
            node["call_sites"] = []
            node["call_sites_truncated"] = False

    def record_call(file, call, target, basis):
        caller = node_by_id.get(call["caller"])
        if not caller:
            return
        if len(caller["call_sites"]) >= 30:
            caller["call_sites_truncated"] = True
            return
        unresolved = None if target else ("complex" if not call.get("base") else "member" if call.get("attribute") else "name")
        caller["call_sites"].append({"written": call["written"], "written_truncated": call.get("written_truncated", False), "written_arguments": call.get("written_arguments"),
            "path": file["path"], "line": call["line"], "snippet": call["snippet"], "snippet_truncated": call.get("snippet_truncated", False), "line_sha256": call["line_sha256"], "sha256": file["digest"],
            "evidence": "static", "resolution": "inferred" if target else "unresolved", "target_id": target, "basis": basis, "unresolved_form": unresolved})

    modules = defaultdict(list)
    for f in files:
        if f["language"] == "python":
            modules[_module(f["path"])].append(f)
    def unique_module(name):
        options = modules.get(name, [])
        return options[0] if len(options) == 1 else None
    for f in files:
        if f["language"] != "python":
            continue
        parsed, ids = f["parsed"], f["ids"]
        bindings = defaultdict(list)
        for imp in parsed["imports"]:
            module = _import_module(f["path"], imp["module"], imp["level"])
            target = unique_module(module) if module is not None else None
            if target and imp["id"] in ids:
                edges.append({"source": imp["id"], "target": target["fid"], "kind": "possible local module",
                              "evidence": "inferred", "path": f["path"], "line": imp["line"],
                              "snippet": imp["snippet"], "sha256": f["digest"]})
            if imp["bound"]:
                bindings[imp["bound"]].append((imp, target))
        for call in parsed["calls"]:
            if call["caller"] not in ids:
                continue
            target_id, basis = None, None
            if call["base"] in bindings:
                choices = bindings[call["base"]]
                if len(choices) == 1:
                    imp, target = choices[0]
                    symbol = imp["symbol"] if imp["symbol"] and not call["attribute"] else call["attribute"]
                    # Dotted imports without an explicit alias bind the package, not the final module.
                    if target and symbol and not (imp["symbol"] and call["attribute"]) and (imp["symbol"] or imp["module"] == imp["bound"] or imp["aliased"]):
                        matches = target["parsed"]["top_level"].get(symbol, [])
                        if len(matches) == 1 and matches[0] in target["ids"]:
                            target_id = matches[0]
                            basis = "unique import and name match; rebinding and runtime dispatch are not resolved"
            elif not call["attribute"]:
                matches = parsed["top_level"].get(call["base"], [])
                if len(matches) == 1 and matches[0] in ids:
                    target_id = matches[0]
                    basis = "unique same-file name match; shadowing and runtime dispatch are not resolved"
            record_call(f, call, target_id, basis)
            if target_id:
                edges.append({"source": call["caller"], "target": target_id, "kind": "possible call",
                              "evidence": "inferred", "path": f["path"], "line": call["line"],
                              "snippet": call["snippet"], "snippet_truncated": call.get("snippet_truncated", False), "sha256": f["digest"], "basis": basis})
    js_files = {f["path"]: f for f in files if f["language"] in ("javascript", "typescript", "tsx", "jsx")}
    def js_target(path, source):
        if not source.startswith(("./", "../")):
            return None
        stem = posixpath.normpath(posixpath.join(posixpath.dirname(path), source))
        if stem == ".." or stem.startswith("../"):
            return None
        choices = [stem] if PurePosixPath(stem).suffix else [stem + x for x in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")] + [stem + "/index" + x for x in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")]
        matches = [js_files[x] for x in choices if x in js_files]
        return matches[0] if len(matches) == 1 else None
    for f in js_files.values():
        parsed, ids = f["parsed"], f["ids"]
        bindings = defaultdict(list)
        linked_imports = set()
        for imp in parsed["imports"]:
            target = js_target(f["path"], imp["source"])
            if target and imp["id"] in ids and imp["id"] not in linked_imports:
                edges.append({"source": imp["id"], "target": target["fid"], "kind": "possible local module",
                              "evidence": "inferred", "path": f["path"], "line": imp["line"],
                              "snippet": imp["snippet"], "sha256": f["digest"]})
                linked_imports.add(imp["id"])
            if imp["bound"]:
                bindings[imp["bound"]].append((imp, target))
        for call in parsed["calls"]:
            if call["caller"] not in ids:
                continue
            target_id, basis = None, None
            if call["base"] in bindings and len(bindings[call["base"]]) == 1:
                imp, target = bindings[call["base"]][0]
                symbol = call["attribute"] if imp["namespace"] else imp["symbol"] if not call["attribute"] else None
                matches = target["parsed"]["exports"].get(symbol, []) if target and symbol else []
                if len(matches) == 1 and matches[0] in target["ids"]:
                    target_id = matches[0]
                    basis = "unique import and export name match; rebinding and runtime loading are not resolved"
            elif call["base"] not in bindings and not call["attribute"]:
                matches = parsed["top_level"].get(call["base"], [])
                if len(matches) == 1 and matches[0] in ids:
                    target_id = matches[0]
                    basis = "unique same-file name match; shadowing and runtime dispatch are not resolved"
            record_call(f, call, target_id, basis)
            if target_id:
                edges.append({"source": call["caller"], "target": target_id, "kind": "possible call",
                              "evidence": "inferred", "path": f["path"], "line": call["line"],
                              "snippet": call["snippet"], "snippet_truncated": call.get("snippet_truncated", False), "sha256": f["digest"], "basis": basis})
    cpp_files = {f["path"]: f for f in files if f["language"] == "cpp"}
    cpp_definitions = defaultdict(list)
    for f in cpp_files.values():
        for name, ids in f["parsed"]["top_level"].items():
            cpp_definitions[name].extend((f, node_id) for node_id in ids if node_id in f["ids"])
    for f in cpp_files.values():
        parsed, ids = f["parsed"], f["ids"]
        for imp in parsed["imports"]:
            if not imp["local"] or imp["id"] not in ids:
                continue
            target_path = posixpath.normpath(posixpath.join(posixpath.dirname(f["path"]), imp["source"]))
            target = cpp_files.get(target_path)
            if target:
                edges.append({"source": imp["id"], "target": target["fid"], "kind": "possible local module",
                              "evidence": "inferred", "path": f["path"], "line": imp["line"],
                              "snippet": imp["snippet"], "sha256": f["digest"]})
        for call in parsed["calls"]:
            if call["caller"] not in ids:
                continue
            matches = cpp_definitions.get(call["base"], []) if not call["attribute"] else []
            target_id = matches[0][1] if len(matches) == 1 else None
            basis = "unique indexed name; compilation and linking are not resolved" if target_id else None
            record_call(f, call, target_id, basis)
            if target_id:
                edges.append({"source": call["caller"], "target": target_id, "kind": "possible call",
                              "evidence": "inferred", "path": f["path"], "line": call["line"],
                              "snippet": call["snippet"], "snippet_truncated": call.get("snippet_truncated", False), "sha256": f["digest"], "basis": basis})
    # Add bounded, selectable written call sites after the file/function inventory.
    # The source node is static syntax; a target edge is still only an inference.
    mapped_calls, unmapped_calls = 0, 0
    for caller in list(nodes):
        for ordinal, call in enumerate(caller.get("call_sites", [])):
            needed_edges = 1 + int(bool(call["target_id"] and call["target_id"] in node_by_id))
            if mapped_calls >= 150 or len(nodes) >= MAX_NODES or len(edges) + needed_edges > MAX_EDGES:
                unmapped_calls += 1
                continue
            fingerprint = hashlib.sha256((call["written"] + "\n" + call["line_sha256"]).encode()).hexdigest()
            stable = hashlib.sha256((caller["id"] + "#" + str(call["line"]) + "#" + fingerprint + "#" + str(ordinal)).encode()).hexdigest()
            call_id = "callsite:" + stable
            call["map_node_id"] = call_id
            call_node = {"id": call_id, "kind": "callsite", "name": call["written"] + (" [callee truncated]" if call["written_truncated"] else ""), "path": call["path"],
                         "line": call["line"], "sha256": call["sha256"], "fingerprint": fingerprint,
                         "language": caller.get("language"), "evidence": "static", "call_sites": [call],
                         "call_sites_truncated": False, "snippet": call["snippet"], "snippet_truncated": call["snippet_truncated"]}
            nodes.append(call_node)
            node_by_id[call_id] = call_node
            edges.append({"source": caller["id"], "target": call_id, "kind": "contains written call", "evidence": "static",
                          "path": call["path"], "line": call["line"], "snippet": call["snippet"],
                          "snippet_truncated": call["snippet_truncated"], "sha256": call["sha256"]})
            if call["target_id"] and call["target_id"] in node_by_id:
                edges.append({"source": call_id, "target": call["target_id"], "kind": "possible target", "evidence": "inferred",
                              "path": call["path"], "line": call["line"], "snippet": call["snippet"],
                              "snippet_truncated": call["snippet_truncated"], "sha256": call["sha256"], "basis": call["basis"]})
            mapped_calls += 1
    if unmapped_calls:
        warnings.append(f"{unmapped_calls} recorded call sites have no map node due to limits; inspect the containing item for its call-site list.")
    if len(edges) > MAX_EDGES:
        edges = edges[:MAX_EDGES]
        warnings.append(f"Connection limit reached ({MAX_EDGES}).")
    inventory = sorted((n["path"], n["sha256"]) for n in nodes if n["kind"] == "file")
    nodes[0]["fingerprint"] = hashlib.sha256(json.dumps([inventory, warnings], ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()
    return {"repo_id": repo_id, "root_name": root.name, "nodes": nodes, "edges": edges,
            "files_inspected": len(files), "warnings": warnings,
            "scope": "Python, a subset of JavaScript, TypeScript/JSX and C++. CommonJS, TypeScript configuration, C++ macros, overload and build/link resolution are not mapped. Imports and includes are declarations; targets and calls are inferred. No runtime calls were observed. Files can change after scanning.",
            "parser_isolation": limits.isolation}


def parse_python(data: bytes, path: str) -> dict:
    """Runs only inside the existing bounded extraction worker."""
    try:
        text = data.decode("utf-8-sig")
        tree = ast.parse(text, filename=path)
        visitor = _Visitor(path, text, hashlib.sha256(data).hexdigest())
        visitor.visit(tree)
        return {"nodes": visitor.nodes, "edges": visitor.edges, "imports": visitor.imports,
                "calls": visitor.calls, "top_level": visitor.top_level, "truncated": visitor.truncated}
    except (UnicodeError, SyntaxError, ValueError, RecursionError, MemoryError):
        return {"error": "invalid or overly complex Python source"}


class _Returns(ast.NodeVisitor):
    def __init__(self):
        self.lines = []
        self.features = []

    def generic_visit(self, node):
        kinds = {ast.If: "conditional", ast.For: "loop", ast.AsyncFor: "loop", ast.While: "loop",
                 ast.Try: "exception handling", ast.TryStar: "exception handling", ast.Raise: "raise",
                 ast.Await: "await", ast.Match: "pattern match"}
        kind = kinds.get(type(node))
        if kind:
            self.features.append({"kind": kind, "line": node.lineno})
        super().generic_visit(node)

    def visit_Return(self, node):
        self.lines.append(node.lineno)
        self.generic_visit(node)

    def visit_FunctionDef(self, node):
        pass

    visit_AsyncFunctionDef = visit_FunctionDef
    visit_ClassDef = visit_FunctionDef
    visit_Lambda = visit_FunctionDef


def _python_shape(node, lines):
    from .repository_learning import evidence_lines
    args = node.args
    entries = [(a, "positional-only") for a in args.posonlyargs]
    entries += [(a, "positional-or-keyword") for a in args.args]
    if args.vararg:
        entries.append((args.vararg, "variadic positional"))
    entries += [(a, "keyword-only") for a in args.kwonlyargs]
    if args.kwarg:
        entries.append((args.kwarg, "variadic keyword"))
    returns = _Returns()
    for statement in node.body:
        returns.visit(statement)
    return {"parameters": [{"name": a.arg, "kind": kind, "line": a.lineno} for a, kind in entries[:30]],
            "parameters_truncated": len(entries) > 30, "parameter_detail": "named",
            "async": isinstance(node, ast.AsyncFunctionDef), "return_lines": returns.lines[:30],
            "returns_truncated": len(returns.lines) > 30, "return_detail": "explicit", "features": returns.features[:30], "features_truncated": len(returns.features) > 30,
            "evidence_lines": evidence_lines(lines, [a.lineno for a, _ in entries[:30]] + returns.lines[:30] + [f["line"] for f in returns.features[:30]])}


class _Visitor(ast.NodeVisitor):
    def __init__(self, path, text, digest):
        self.path, self.text, self.lines, self.digest = path, text, text.splitlines(), digest
        self.byte_lines = [line.encode("utf-8") for line in self.lines]
        self.file_id = "file:" + path
        self.nodes, self.edges, self.calls, self.imports = [], [], [], []
        self.top_level, self.names, self.stack = defaultdict(list), {}, []
        self.truncated = False

    def _line(self, line):
        return self.lines[line - 1][:1000] if 0 < line <= len(self.lines) else ""

    def visit_ClassDef(self, node):
        self.stack.append((node.name, None))
        self.generic_visit(node)
        self.stack.pop()

    def visit_FunctionDef(self, node):
        if len(self.nodes) >= MAX_NODES:
            self.truncated = True
            return
        qual = ".".join([x[0] for x in self.stack] + [node.name])
        key = "function:" + self.path + ":" + qual
        self.names[key] = self.names.get(key, 0) + 1
        if self.names[key] > 1:
            key += "#" + str(self.names[key])
        parent = next((x[1] for x in reversed(self.stack) if x[1]), self.file_id)
        fingerprint = hashlib.sha256(ast.dump(node, include_attributes=False).encode()).hexdigest()
        snippet = "\n".join(self._line(i) for i in range(node.lineno, min(node.end_lineno or node.lineno, node.lineno + 11) + 1))
        self.nodes.append({"id": key, "kind": "function", "name": qual, "path": self.path,
                           "line": node.lineno, "end_line": node.end_lineno, "snippet": snippet,
                           "sha256": self.digest, "fingerprint": fingerprint, "language": "python",
                           "shape": _python_shape(node, self.lines), "evidence": "static"})
        self.edges.append({"source": parent, "target": key, "kind": "defines", "evidence": "static", "path": self.path, "line": node.lineno})
        if not self.stack:
            self.top_level[node.name].append(key)
        self.stack.append((node.name, key))
        for child in node.body:
            self.visit(child)
        self.stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def _written_callee(self, node):
        start = node.lineno - 1
        end = (getattr(node, "end_lineno", None) or node.lineno) - 1
        end_col = getattr(node, "end_col_offset", None)
        chunks, size, incomplete = [], 0, False
        for index in range(start, min(end + 1, len(self.byte_lines))):
            line = self.byte_lines[index]
            left = node.col_offset if index == start else 0
            right = end_col if index == end and end_col is not None else len(line)
            piece = line[left:right]
            remaining = 804 - size
            if remaining <= 0:
                incomplete = True
                break
            if len(piece) > remaining:
                piece, incomplete = piece[:remaining], True
            chunks.append(piece)
            size += len(piece)
            if incomplete:
                break
        value = b"\n".join(chunks).decode("utf-8", errors="ignore") or "[complex call]"
        return value[:200], incomplete or len(value) > 200

    def visit_Call(self, node):
        base, attribute = None, None
        if isinstance(node.func, ast.Name):
            base = node.func.id
        elif isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
            base, attribute = node.func.value.id, node.func.attr
        if len(self.calls) < MAX_EDGES:
            caller = next((x[1] for x in reversed(self.stack) if x[1]), self.file_id)
            written, written_truncated = self._written_callee(node.func)
            self.calls.append({"caller": caller, "base": base, "attribute": attribute, "written": written, "written_truncated": written_truncated,
                               "written_arguments": len(node.args) + len(node.keywords),
                               "line": node.lineno, "snippet": self._line(node.lineno),
                               "snippet_truncated": len(self.lines[node.lineno - 1]) > 1000,
                               "line_sha256": hashlib.sha256(self.byte_lines[node.lineno - 1]).hexdigest()})
        else:
            self.truncated = True
        self.generic_visit(node)

    def visit_Import(self, node):
        for alias in node.names:
            self._import(alias.name, 0, None, alias.asname or alias.name.split(".")[0], bool(alias.asname), node.lineno)

    def visit_ImportFrom(self, node):
        for alias in node.names:
            self._import(node.module or "", node.level, alias.name, None if alias.name == "*" else alias.asname or alias.name,
                         bool(alias.asname), node.lineno)

    def _import(self, module, level, symbol, bound, aliased, line):
        if len(self.nodes) >= MAX_NODES:
            self.truncated = True
            return
        name = "." * level + module + ((" import " + symbol) if symbol else "")
        key = f"import:{self.path}:{line}:{name}"
        self.names[key] = self.names.get(key, 0) + 1
        if self.names[key] > 1:
            key += "#" + str(self.names[key])
        self.nodes.append({"id": key, "kind": "dependency", "name": name, "path": self.path,
                           "line": line, "sha256": self.digest, "evidence": "static"})
        self.edges.append({"source": self.file_id, "target": key, "kind": "imports", "evidence": "static", "path": self.path, "line": line})
        self.imports.append({"id": key, "module": module, "level": level, "symbol": symbol, "bound": bound,
                             "aliased": aliased, "line": line, "snippet": self._line(line)})

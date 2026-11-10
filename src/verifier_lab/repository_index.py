"""Per-scan lookup tables so every question about one item costs that item's size, not the repository's.

The index adds no facts. It only regroups the nodes and edges a scan already produced.
"""
from __future__ import annotations

from collections import defaultdict


class RepositoryIndex:
    def __init__(self, nodes, edges, *, complete=True):
        self.nodes = nodes if isinstance(nodes, dict) else {node["id"]: node for node in nodes}
        self.edges = edges
        self.complete = complete  # False when the scan dropped connections at its limit
        self._incident = defaultdict(list)
        self.callers = defaultdict(list)  # target id -> inferred call edges, in a stable order
        self.by_path = defaultdict(list)
        self.unresolved_calls = 0
        self.partial_call_lists = False
        self._folders = None
        self._paths = None
        self._search_rows = None
        for edge in edges:
            source, target = edge.get("source"), edge.get("target")
            self._incident[source].append(edge)
            if target != source:
                self._incident[target].append(edge)
            if edge.get("kind") == "possible call" and edge.get("evidence") == "inferred" and source in self.nodes and target in self.nodes:
                self.callers[target].append(edge)
        for sites in self.callers.values():
            if len(sites) > 1:
                sites.sort(key=lambda item: (item.get("path") or "", item.get("line") or 0, item["source"], item.get("written") or ""))
        for node in self.nodes.values():
            path = node.get("path")
            if path and node["kind"] != "file":
                self.by_path[path].append(node["id"])
            if node["kind"] in ("file", "function"):
                self.partial_call_lists = self.partial_call_lists or bool(node.get("call_sites_truncated"))
                self.unresolved_calls += sum(call.get("resolution") == "unresolved" for call in node.get("call_sites", []))

    # ------------------------------------------------------------------ edges
    def incident(self, node_id):
        """Every recorded connection touching this item, in scan order."""
        return self._incident.get(node_id, [])

    def outgoing(self, node_id):
        return [edge for edge in self._incident.get(node_id, []) if edge["source"] == node_id]

    def incoming(self, node_id):
        return [edge for edge in self._incident.get(node_id, []) if edge["target"] == node_id]

    def degree(self, node_id):
        return len(self._incident.get(node_id, []))

    def neighbors(self, node_id):
        found = set()
        for edge in self._incident.get(node_id, []):
            other = edge["target"] if edge["source"] == node_id else edge["source"]
            if other in self.nodes:
                found.add(other)
        return found

    # ------------------------------------------------------------------ items
    def in_file(self, path):
        """Ids of functions, dependencies and written calls recorded in one file."""
        return self.by_path.get(path, [])

    def at(self, path, line):
        return [ident for ident in self.by_path.get(path, ()) if self.nodes[ident].get("line") == line]

    def callers_of(self, target):
        """Possible callers of one function, each with the written call that suggests it."""
        found = []
        for edge in self.callers.get(target, ()):
            via = {key: edge.get(key) for key in ("written", "path", "line", "snippet", "sha256", "basis")}
            via.update({"snippet_truncated": bool(edge.get("snippet_truncated")), "source": edge["source"], "target": target,
                        "evidence": "inferred", "call_evidence": "static"})
            found.append(via)
        return found

    def enclosing(self, path, line):
        """The innermost recorded function containing a line, else the file."""
        best = None
        for ident in self.by_path.get(path, []):
            node = self.nodes[ident]
            if node["kind"] != "function" or type(node.get("line")) is not int:
                continue
            end = node.get("end_line") if type(node.get("end_line")) is int else node["line"]
            if node["line"] <= line <= end and (best is None or node["line"] >= self.nodes[best]["line"]):
                best = ident
        return best or ("file:" + path if "file:" + path in self.nodes else None)

    # ------------------------------------------------------------------ browsing
    def folders(self):
        """folder path -> (sorted child folder names, sorted file ids). The root folder is ''."""
        if self._folders is None:
            children, files = defaultdict(set), defaultdict(list)
            for node in self.nodes.values():
                if node["kind"] != "file":
                    continue
                parts = node["path"].split("/")
                files["/".join(parts[:-1])].append(node["id"])
                for depth in range(len(parts) - 1):
                    children["/".join(parts[:depth])].add(parts[depth])
            self._folders = {folder: (sorted(children.get(folder, ()), key=str.casefold), sorted(files.get(folder, ())))
                             for folder in set(children) | set(files) | {""}}
        return self._folders

    def paths(self):
        """Paths of every listed file."""
        if self._paths is None:
            self._paths = [node["path"] for node in self.nodes.values() if node["kind"] == "file"]
        return self._paths

    def file_items(self, path):
        """Functions and dependencies in a file by line; written calls are listed under their function."""
        items = [self.nodes[ident] for ident in self.by_path.get(path, []) if self.nodes[ident]["kind"] != "callsite"]
        return sorted(items, key=lambda node: (node.get("line") or 0, node["id"]))

    def search(self, query, limit=500):
        """Case-insensitive match on name, label, path and kind. Returns (ids, total matches)."""
        query = query.casefold().strip()
        if not query:
            return [], 0
        if self._search_rows is None:
            self._search_rows = [(node["id"], " ".join(str(node.get(key) or "") for key in ("name", "path", "kind")).casefold())
                                 for node in self.nodes.values()]
        found, total = [], 0
        for ident, text in self._search_rows:
            if query in text or query in str(self.nodes[ident].get("label") or "").casefold():
                total += 1
                if len(found) < limit:
                    found.append(ident)
        return found, total

"""Bounded comparison of two in-memory repository scan snapshots."""
from __future__ import annotations

from collections import Counter

MAX_CHANGE_ITEMS = 100
EXCERPT_LINES = 3
EXCERPT_CHARS = 200


def _excerpt(node):
    if node["kind"] == "function":
        start = node.get("line")
        if type(start) is not int:
            return []
        return [{"line": start + i, "text": line[:EXCERPT_CHARS], "truncated": len(line) > EXCERPT_CHARS}
                for i, line in enumerate((node.get("snippet") or "").splitlines()[:EXCERPT_LINES])]
    return [{"line": line["line"], "text": line["text"][:EXCERPT_CHARS],
             "truncated": bool(line.get("truncated") or len(line["text"]) > EXCERPT_CHARS)}
            for line in node.get("source_preview", [])[:EXCERPT_LINES]]


def baseline_of(scan):
    """What a later comparison needs from a scan, without holding the scan itself."""
    kept = []
    for node in scan["nodes"]:
        if node.get("kind") in ("file", "function"):
            slim = {key: node.get(key) for key in ("id", "kind", "name", "path", "line", "sha256", "fingerprint", "label")}
            if node["kind"] == "function":
                slim["snippet"] = "\n".join((node.get("snippet") or "").splitlines()[:EXCERPT_LINES])
            else:
                slim["source_preview"] = list(node.get("source_preview", [])[:EXCERPT_LINES])
            kept.append(slim)
    return {"location_id": scan.get("location_id"), "repo_id": scan.get("repo_id"), "nodes": kept}


def compare_scans(previous, current):
    """Compare known file bytes and extracted function fingerprints, not behavior."""
    if (not previous.get("location_id") or previous.get("location_id") != current.get("location_id") or
            not previous.get("repo_id") or previous.get("repo_id") != current.get("repo_id")):
        raise ValueError("scan locations or saved collections differ")
    old = {node["id"]: node for node in previous["nodes"] if node.get("kind") in ("file", "function")}
    new = {node["id"]: node for node in current["nodes"] if node.get("kind") in ("file", "function")}
    entries = []
    for ident in old.keys() | new.keys():
        before, after = old.get(ident), new.get(ident)
        if before is None:
            status = "added"
        elif after is None:
            status = "removed"
        elif before.get("fingerprint") and after.get("fingerprint") and before["fingerprint"] != after["fingerprint"]:
            status = "changed"
        elif before.get("fingerprint") and before.get("fingerprint") == after.get("fingerprint") and before.get("line") != after.get("line"):
            status = "line moved"
        else:
            continue
        display = after or before
        entries.append((status, ident, display.get("path") or "", display.get("line") or 0))
    order = {"changed": 0, "added": 1, "removed": 2, "line moved": 3}
    entries.sort(key=lambda entry: (order[entry[0]], entry[2], entry[3], entry[1]))
    counts = Counter(entry[0] for entry in entries)

    def snapshot(node):
        if node is None: return None
        return {"name": node.get("name"), "path": node.get("path"), "line": node.get("line"),
                "sha256": node.get("sha256"), "excerpt": _excerpt(node)}

    changes = [{"status": status, "node_id": ident, "kind": (new.get(ident) or old[ident])["kind"],
                "label": (new.get(ident) or old[ident]).get("label") or "",
                "before": snapshot(old.get(ident)), "after": snapshot(new.get(ident))}
               for status, ident, _, _ in entries[:MAX_CHANGE_ITEMS]]
    return {"counts": {key: counts[key] for key in order}, "changes": changes,
            "total": len(entries), "truncated": len(entries) > MAX_CHANGE_ITEMS,
            "limitation": "This compares scanned file bytes and comparable extracted function fingerprints, not behavior. "
                          "Added and removed mean recorded in one scan only; moves, renames or scan omissions may also explain them. "
                          "Identifiers may be reused, and matching fingerprints do not prove unchanged behavior. "
                          "Excerpts are bounded snapshots and may not show the exact change. No runtime calls were observed."}

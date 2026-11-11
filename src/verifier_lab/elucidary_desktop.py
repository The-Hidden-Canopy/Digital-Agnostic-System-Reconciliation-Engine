"""Elucidary native desktop: local service calls, no browser or HTTP listener."""
from __future__ import annotations

import argparse
import math
import os
import queue
import sys
import threading
from pathlib import Path

from .ingestion.common import Limits
from .service import Service, ServiceError


def default_workspace():
    base = os.environ.get("LOCALAPPDATA") if sys.platform == "win32" else None
    return Path(base) / "Elucidary" if base else Path.home() / ".elucidary"


def graph_view(nodes, edges, selected, route=(), limit=80, overview=False, focus_path=None, labeled=False, index=None):
    """Select a bounded graph and lay it out in file clusters, with no inferred edges added.

    With a scan index the choice costs the size of the neighbourhood shown, not of the repository;
    nodes then only needs to hold the labeled items.
    """
    by_id = index.nodes if index is not None else {n["id"]: n for n in nodes}
    chosen, seen = [], set()

    def add(ident):
        if ident in by_id and ident not in seen:
            seen.add(ident)
            chosen.append(ident)

    add(selected)
    for ident in route:
        add(ident)
    limit = max(limit, len(chosen))
    if focus_path is not None:
        members = (node for node in nodes if node.get("path") == focus_path)
        if index is not None:
            members = [by_id[i] for i in index.in_file(focus_path)] + ([by_id["file:" + focus_path]] if "file:" + focus_path in by_id else [])
        same_file = sorted(members, key=lambda n: (n["kind"] != "file", not bool(n.get("label")), n["id"]))
        for node in same_file:
            if len(chosen) >= limit: break
            add(node["id"])
    elif labeled:
        labeled_nodes = sorted((node for node in nodes if node.get("label")), key=lambda n: (n.get("path") or "", n["id"]))
        for node in labeled_nodes:
            if len(chosen) >= limit: break
            add(node["id"])
        # Give labeled items their immediate, already-recorded graph context.
        neighbors = {ident: set() for ident in chosen}
        for edge in (edges if index is None else [e for ident in chosen for e in index.incident(ident) if graph_edge_shown(e, edges)]):
            a, b = edge.get("source"), edge.get("target")
            if a in neighbors and b in by_id: neighbors[a].add(b)
            if b in neighbors and a in by_id: neighbors[b].add(a)
        for ident in list(chosen):
            for neighbor in sorted(neighbors[ident]):
                if len(chosen) >= limit: break
                add(neighbor)
    elif overview and len(chosen) < limit:
        groups = {}
        if index is not None:
            # Only the first groups in path order can be reached before the limit.
            for key in sorted(index.paths() + ["Repository"])[:limit]:
                groups[key] = ([by_id["repo"]] if key == "Repository" and "repo" in by_id else
                               [by_id[i] for i in ["file:" + key] + list(index.in_file(key)) if i in by_id])
        for node in (nodes if index is None else ()):
            groups.setdefault(node.get("path") or "Repository", []).append(node)
        ordered = [sorted(group, key=lambda n: (not bool(n.get("label")), n["kind"] != "file", n["id"]))
                   for _, group in sorted(groups.items())]
        for i in range(max((len(group) for group in ordered), default=0)):
            found = False
            for group in ordered:
                if i < len(group):
                    found = True
                    add(group[i]["id"])
                if len(chosen) >= limit:
                    break
            if not found or len(chosen) >= limit:
                break
    elif not overview:
        adjacent = {} if index is not None else {ident: [] for ident in by_id}
        for edge in (edges if index is None else ()):
            a, b = edge.get("source"), edge.get("target")
            if a in adjacent and b in adjacent:
                adjacent[a].append(b)
                adjacent[b].append(a)

        def around(ident):
            if index is None:
                return adjacent[ident]
            return {(e["target"] if e["source"] == ident else e["source"]) for e in index.incident(ident)
                    if graph_edge_shown(e, edges) and e["source"] in by_id and e["target"] in by_id}
        cursor = 0
        while cursor < len(chosen) and len(chosen) < limit:
            for ident in sorted(around(chosen[cursor])):
                add(ident)
                if len(chosen) >= limit:
                    break
            cursor += 1
    groups = {}
    for ident in chosen:
        node = by_id[ident]
        groups.setdefault(node.get("path") or "Repository", []).append(ident)
    ordered = sorted(groups.items())
    columns = max(1, math.ceil(math.sqrt(len(ordered) * 1.6)))
    rows = max(1, math.ceil(len(ordered) / columns))
    cell_w, cell_h = 840 / columns, 460 / rows
    positions = {}
    for index, (_, group) in enumerate(ordered):
        x, y = 30 + cell_w * (index % columns + .5), 30 + cell_h * (index // columns + .5)
        radius = max(0, min(cell_w, cell_h) * .38 - 5)
        for i, ident in enumerate(group):
            angle = i * 2.399963
            r = radius * math.sqrt(i / (len(group) - 1)) if len(group) > 1 else 0
            positions[ident] = (x + math.cos(angle) * r, y + math.sin(angle) * r)
    return chosen, positions


def graph_edge_shown(edge, shown):
    """With an index, the edges argument is the set of evidence classes to draw."""
    return edge.get("evidence") in shown


def graph_constellations(nodes, chosen, projected, selected, limit=12):
    """Bounded visual file groups; group boundaries do not assert relationships."""
    groups, by_id = {}, (nodes if isinstance(nodes, dict) else {node["id"]: node for node in nodes})
    for ident in chosen:
        node = by_id[ident]; path = node.get("path")
        if path and ident in projected:
            groups.setdefault(path, []).append(ident)
    selected_path = by_id.get(selected, {}).get("path")
    ordered = sorted(groups.items(), key=lambda item: (item[0] != selected_path, -len(item[1]), item[0]))
    result = []
    for path, members in ordered[:limit]:
        xs, ys = zip(*(projected[ident] for ident in members))
        result.append((path, (min(xs) - 18, min(ys) - 18, max(xs) + 18, max(ys) + 18), len(members)))
    return result


def graph_labels(nodes, points, selected, route, width, height, limit=12):
    """Prioritize readable labels while leaving the accessible item list complete."""
    route = set(route); placed, boxes = [], []
    def rank(node):
        return (0 if node["id"] == selected else 1 if node["id"] in route else 2 if node.get("label") else 3 if node["kind"] == "file" else 4, node["id"])
    for node in sorted(nodes, key=rank):
        if len(placed) >= limit: break
        x, y = points[node["id"]]
        if x < -10 or y < -10 or x > width + 10 or y > height + 10: continue
        full = ("! " if node.get("label") and node.get("label_needs_review") else "") + str(node.get("label") or node.get("name") or node["kind"])
        text = full if len(full) <= 26 else full[:23] + "…"
        extent = min(190, 7 * len(text) + 4)
        x = max(2, min(max(2, width - extent - 2), x + 10)); y = max(14, min(height - 3, y - 8))
        box = (x, y - 13, x + extent, y + 3)
        if any(box[0] < b[2] and box[2] > b[0] and box[1] < b[3] and box[3] > b[1] for b in boxes): continue
        boxes.append(box); placed.append((node["id"], text, x, y))
    return placed


def describe_connection(edge, outgoing, level):
    """Explain a graph relationship without treating static inference as execution."""
    kind, evidence = edge.get("kind"), edge.get("evidence")
    explanations = {
        "contains": "The scanned project contains this file.",
        "defines": "The source contains this definition.",
        "imports": "The source writes an import of this dependency.",
        "includes": "The source writes an include of this dependency.",
        "contains written call": "The source contains a written call expression.",
        "possible call": "A written call may refer to this function.",
        "possible target": "This function may be the target of the written call.",
        "possible local module": "The written dependency may refer to this local file.",
    }
    text = explanations.get(kind, "The scan recorded a connection between these items.")
    direction = "The arrow starts at the selected item." if outgoing else "The arrow points to the selected item."
    if evidence == "inferred":
        caveat = {"beginner": "This is a possible link, not proof that anything ran.",
                  "explore": "This is a source-based possibility. Compare both locations and consider what else could be connected.",
                  "technical": "This target is statically inferred; rebinding, dispatch, loading and execution are not established."}[level]
    elif evidence == "static":
        caveat = {"beginner": "This is based on what was found in source; it does not prove the code ran.",
                  "explore": "Find the relationship in the recorded source and compare it with the linked item.",
                  "technical": "This is recorded syntax or containment, not a runtime observation or proof of reachability."}[level]
    else:
        caveat = "The scan did not establish a supported evidence classification for this link."
    return " ".join((text, direction, caveat))


class DesktopController:
    """State shared by the desktop widgets; all repository operations go through Service."""

    def __init__(self, service):
        self.service = service
        self.data = None
        self.nodes = {}
        self.selected = None
        self.audience = None
        self.route = None
        self.index = None
        self.scan_limits = None  # None: the desktop bounds for the chosen options

    def clear(self):
        if self.data:
            try: self.service.repository_release(self.data.get("repo_id"), self.data.get("scan_id"))
            except Exception: pass
        self.data, self.nodes, self.selected, self.audience, self.route, self.index = None, {}, None, None, None, None

    def open(self, path, audience, isolation, baseline=None, *, progress=None, should_cancel=None,
             include_vendored=False, include_generated=False):
        from .repository_map import ScanLimits
        self.clear()
        if audience not in ("adult", "adult_reviewed"):
            raise ServiceError(403, "adult_review_required", "Ask an adult to review this repository before opening it together.")
        if isolation not in ("required", "portable"):
            raise ServiceError(422, "bad_isolation")
        self.service.limits = Limits(isolation=isolation)
        limits = self.scan_limits or ScanLimits.desktop(include_vendored=include_vendored, include_generated=include_generated)
        result = self.service.repository_map(path, content_access=audience, scan_limits=limits, progress=progress,
                                             should_cancel=should_cancel, use_cache=True)
        if audience == "adult_reviewed":
            result["saved_projects"] = []
        if baseline is not None:
            previous, previous_audience = baseline
            if (previous_audience == audience and previous.get("location_id") == result.get("location_id") and
                    previous.get("repo_id") == result.get("repo_id")):
                from .repository_changes import compare_scans
                result["comparison"] = compare_scans(previous, result)
            else:
                result["comparison_unavailable"] = True
        self.data, self.nodes, self.selected, self.audience, self.route = result, {n["id"]: n for n in result["nodes"]}, "repo", audience, None
        from .repository_index import RepositoryIndex
        # The service built this index for the scan; the fallback keeps a replaced service usable.
        self.index = self.service.repository_indexes.get(result["repo_id"]) or RepositoryIndex(self.nodes, result["edges"])
        return result

    def context(self):
        if not self.data or self.selected not in self.nodes:
            raise ServiceError(409, "repository_unavailable", "Open a repository first.")
        return self.data["repo_id"], self.selected, self.data["scan_id"]

    def select(self, ident):
        if ident not in self.nodes:
            raise ServiceError(409, "stale_repository")
        self.selected = ident
        return self.nodes[ident]

    def explain(self, topic, level, use_model=False):
        repo, ident, sid = self.context()
        return self.service.repository_explain(repo, ident, topic, level, sid, use_local_assistant=use_model)

    def label(self, label):
        repo, ident, sid = self.context()
        result = self.service.repository_label(repo, ident, label, sid)
        self.nodes[ident]["label"] = result["label"]
        self.nodes[ident]["label_needs_review"] = False
        self.route = None
        return result

    def guide(self, goal, level, prefer_unreviewed=False):
        repo, ident, sid = self.context()
        self.route = self.service.repository_guided_path(repo, ident, goal, level, prefer_unreviewed, sid)
        return self.route

    def path_to_label(self, target, level, include_inferred=False, include_organization=True):
        repo, ident, sid = self.context()
        self.route = self.service.repository_label_path(repo, ident, target, level, include_inferred, include_organization, sid)
        return self.route

    def path_between(self, start, target, level, include_inferred=False, include_organization=False):
        repo, _, sid = self.context()
        self.route = self.service.repository_item_path(repo, start, target, level, include_inferred, include_organization, sid)
        return self.route

    def compare(self, index, level):
        repo, ident, sid = self.context()
        return self.service.repository_compare_call(repo, ident, index, level, sid)

    def remember(self):
        repo, _, sid = self.context()
        return self.service.repository_remember(repo, sid)

    def source(self, ident=None):
        """The whole scanned file for an item, only while its bytes still match the scan."""
        repo, selected, sid = self.context()
        return self.service.repository_source(repo, ident or selected, sid)

    def search_source(self, query, should_cancel=None, progress=None):
        repo, _, sid = self.context()
        return self.service.repository_search_source(repo, query, sid, should_cancel=should_cancel, progress=progress)

    def shelf(self, audience):
        if audience != "adult":
            raise ServiceError(403, "adult_session_required", "Remembered folders are available in adult sessions only.")
        return self.service.repository_shelf()["items"]


def format_explanation(result):
    lines = list(result["facts"])
    source = result.get("source") or {}
    if source.get("path"):
        lines.append(f"\nSource: {source['path']}:{source.get('line') or '?'} · SHA-256 {source.get('sha256') or 'unavailable'}")
    for citation in result.get("citations", []):
        lines.append(f"{citation['line']}: {citation['text']}" + (" [line shortened]" if citation.get("truncated") else ""))
    for card in result.get("evidence_cards", []):
        c = card["citation"]
        lines.append(f"\n{card['prompt']}\n{c['path']}:{c['line']} ({c['evidence']}): {c['text']}" + (" [excerpt shortened]" if c.get("truncated") else ""))
    for site in result.get("call_sites", []):
        resolution = "possible target (inferred)" if site.get("target_id") else "target unresolved"
        lines.append(f"\nWritten call: {site.get('written')} · {site.get('path')}:{site.get('line')} · static; {resolution}")
        if site.get("snippet"):
            lines.append(site["snippet"] + (" [excerpt shortened]" if site.get("snippet_truncated") else ""))
        if site.get("basis"):
            lines.append(site["basis"])
        elif not site.get("target_id"):
            from .repository_learning import explain_unresolved_call
            lines.append(explain_unresolved_call(site, result["level"]))
        if site.get("sha256"):
            lines.append("Source SHA-256: " + site["sha256"])
    for link in result.get("connections", []):
        lines.append(f"\n{link['direction']}: {link['kind']} ({link['evidence']}) · {link.get('path') or 'location unavailable'}:{link.get('line') or '?'}")
        if link.get("basis"):
            lines.append(link["basis"])
        if link.get("snippet"):
            lines.append(link["snippet"] + (" [excerpt shortened]" if link.get("snippet_truncated") else ""))
        if link.get("sha256"):
            lines.append("Source SHA-256: " + link["sha256"])
    assistant = result.get("assistant")
    if assistant:
        if assistant["status"] == "ready":
            lines.append("\nLocal model interpretation (may be wrong):\n" + assistant["text"])
            for c in assistant["citations"]:
                lines.append(f"[{c['id']}] {c['path']}:{c['line']} ({c['evidence']}): {c['text']}" + (" [excerpt shortened]" if c.get("truncated") else ""))
            lines.append(assistant["limitation"])
        else:
            lines.append("\nLocal model unavailable; the deterministic guide remains available.")
    lines.append("\n" + result["limitation"])
    return "\n".join(lines)


def format_repository_overview(data):
    nodes, edges = data["nodes"], data["edges"]
    count = lambda kind: sum(n["kind"] == kind for n in nodes)
    coverage = data.get("coverage") or {}
    found = coverage.get("supported_files_found")
    reach = ""
    if found is not None:
        reach = (f"Coverage: {data['files_inspected']} of {'at least ' if coverage.get('supported_files_found_is_lower_bound') else ''}{found} "
                 f"supported source files were parsed; {coverage.get('unsupported_files', 0)} files of other types were not read. "
                 "See the Coverage tab for what was skipped and why.\n")
    return (f"Repository snapshot: {data['files_inspected']} files inspected, {count('function')} functions, "
            f"{count('dependency')} dependency items and {count('callsite')} mapped written calls.\n"
            f"{sum(e['evidence'] == 'static' for e in edges)} source links and "
            f"{sum(e['evidence'] == 'inferred' for e in edges)} inferred links; {sum(bool(n.get('label')) for n in nodes)} labeled items.\n" + reach +
            f"{len(data['warnings'])} scan warnings. These are bounded scan counts; no runtime calls were observed.\n\n")


SKIP_REASONS = {
    "hidden_directory": "Folders whose name starts with a dot (not opened)",
    "hidden_file": "Files whose name starts with a dot (not read)",
    "vendor_or_build_directory": "Dependency, build-output or virtual-environment folders (not opened)",
    "bytecode_cache_directory": "Python bytecode cache folders (not opened)",
    "linked_directory": "Folder links or junctions (never followed)",
    "linked_file": "File links (never followed)",
    "unreadable_directory": "Folders the scan could not list",
    "unreadable": "Files that could not be read",
    "oversized": "Files over the size limit (listed, not parsed)",
    "generated": "Generated or minified files (listed, not parsed)",
    "binary": "Files with a source extension but non-text content",
    "could_not_parse": "Files the parser could not read as that language",
    "partial_syntax": "Files parsed with syntax-error regions left out",
    "encoding": "Files that needed a text-encoding fallback",
    "item_limit_in_file": "Files with more items or calls than the per-file limit",
    "node_limit": "Files left out when the total item limit was reached",
}


def format_coverage(data, cache=None):
    """What the scan covered, skipped and assumed, so an absent item is not mistaken for absent code."""
    coverage = data.get("coverage")
    if not coverage:
        return "No coverage summary was recorded for this scan."
    lower = "at least " if coverage["supported_files_found_is_lower_bound"] else ""
    lines = [f"Parsed {coverage['files_parsed']} of {lower}{coverage['supported_files_found']} supported source files in {coverage['seconds']['total']} seconds."]
    if coverage["files_not_inspected_due_to_file_limit"]:
        lines.append(f"{coverage['files_not_inspected_due_to_file_limit']} supported files were found after the {coverage['limits']['files']}-file limit and were NOT inspected. "
                     "Open a subfolder as its own repository to cover them.")
    if coverage["supported_files_found_is_lower_bound"]:
        lines.append(f"The folder walk stopped at {coverage['limits']['directories']} folders, so more files may exist than were counted.")
    if coverage["files_listed_without_parsing"]:
        lines.append(f"{coverage['files_listed_without_parsing']} supported files are listed without functions or calls (see reasons below).")
    lines.append(f"Reused {coverage['cache']['reused']} unchanged files from the local scan cache; parsed {coverage['cache']['parsed_now']} now.")
    lines.append("\nBy language (found / parsed / with syntax-error regions / not parsed):")
    for language, row in coverage["by_language"].items():
        lines.append(f"  {language:<11} {row['found']:>7} / {row['parsed']:>7} / {row['partial']:>5} / {row['not_parsed']:>5}")
    if not coverage["by_language"]:
        lines.append("  No supported source files were found.")
    if coverage["top_level"]:
        lines.append("\nBy top-level folder (supported files found / inspected):")
        for row in sorted(coverage["top_level"], key=lambda item: (item["inspected"] == item["found"], -item["found"], item["name"]))[:40]:
            mark = "" if row["inspected"] == row["found"] else "   <- not fully inspected"
            lines.append(f"  {row['name'][:48]:<48} {row['found']:>7} / {row['inspected']:>7}{mark}")
        if len(coverage["top_level"]) > 40:
            lines.append(f"  ... and {len(coverage['top_level']) - 40} more folders")
    if coverage["skipped"]:
        lines.append("\nPassed over or limited, with examples:")
        for reason, entry in sorted(coverage["skipped"].items(), key=lambda item: -item[1]["count"]):
            lines.append(f"  {entry['count']:>7}  {SKIP_REASONS.get(reason, reason)}")
            for example in entry["examples"][:4]:
                lines.append(f"             {example}")
    if coverage["unsupported_files"]:
        kinds = ", ".join(f"{suffix} {count}" for suffix, count in coverage["unsupported_extensions"].items())
        lines.append(f"\nFiles of unsupported types (not read): {coverage['unsupported_files']}. Most common: {kinds}")
    if coverage["nested_repositories"]:
        lines.append("\nFolders inside this one that are their own Git repositories (scanned as part of this folder): " +
                     ", ".join(coverage["nested_repositories"][:12]) + (" ..." if len(coverage["nested_repositories"]) > 12 else ""))
    duplicates = coverage["duplicate_content"]
    if duplicates["groups"]:
        lines.append(f"\nByte-identical copies: {duplicates['files']} files in {duplicates['groups']} groups, for example " +
                     "; ".join(" = ".join(group) for group in duplicates["examples"][:3]) +
                     ". Copies make names ambiguous, so calls to them stay unresolved.")
    if cache:
        lines.append(f"\nLocal scan cache: {cache['records']} file records, {cache['bytes'] / 1e6:.1f} MB in this workspace. It holds excerpts of scanned source.")
    crossing = coverage.get("cross_repository") or {"links": 0, "examples": []}
    if coverage["nested_repositories"]:
        lines.append(f"\nRelationships between repositories: this folder holds {len(coverage['nested_repositories'])} nested Git repositories, and "
                     f"{crossing['links']} inferred links join files in different ones. They come from the same name and path rules as every other "
                     "inferred link; no package manifest, build file, submodule pin or installed dependency was read, so treat them as leads to check.")
        lines.extend("    " + example for example in crossing["examples"][:6])
    else:
        lines.append("\nRelationships between repositories are not mapped from a single-repository scan: an import or include that leaves this folder "
                     "is recorded as written, with no target. Opening a parent folder that holds both repositories lets the name and path rules "
                     "suggest links between them, as inferences.")
    lines.append("A missing item here means it was not scanned or not recognized, not that it does not exist.")
    return "\n".join(lines)


def format_comparison(result):
    call, target = result["call"], result["target"]
    params = target["parameters"]
    lines = [result["prompt"], f"\nWritten call (static): {call['path']}:{call['line']} · SHA-256 {call.get('sha256')}",
             call.get("snippet") or "[excerpt unavailable]"]
    if call.get("written_arguments") is not None:
        count = call["written_arguments"]
        lines.append(f"The scan recorded {count} written argument expression{'s' if count != 1 else ''}; their runtime values are unknown.")
    lines.append(f"\nPossible target (inferred): {target['path']}:{target['line']} · SHA-256 {target.get('sha256')}")
    lines.extend(f"{line['line']}: {line['text']}" + (" [shortened]" if line["truncated"] else "") for line in target["definition"])
    parameter_text = "details not extracted" if params is None else ", ".join(p["name"] for p in params) if params else "no parameters recorded"
    lines.append("\nParameters: " + parameter_text)
    if target.get("parameters_truncated") or target.get("definition_truncated") or call.get("snippet_truncated") or call.get("written_truncated"):
        lines.append("Some source details were truncated.")
    if result["relationship"].get("basis"):
        lines.append(result["relationship"]["basis"])
    lines.append("\n" + result["limitation"])
    return "\n".join(lines)


class DesktopApp:
    def __init__(self, root, controller):
        import tkinter as tk
        from tkinter import ttk

        self.tk, self.ttk, self.root, self.controller = tk, ttk, root, controller
        self.jobs, self.busy, self.closing = queue.Queue(), False, False
        self.progress_reports, self.cancel_event, self.search_after = queue.Queue(), None, None
        self.tree_rows, self.tree_folder_rows, self.tree_pending = {}, {}, {}
        self.source_shown, self.overview_text, self.labeled_nodes, self.graph_cache, self.match_ids = None, "", [], None, {}
        self.tree_ids, self.source_ids, self.call_indices, self.dest_ids, self.label_inventory_ids = {}, {}, [], [], {}
        self.connection_ids, self.history, self.change_ids, self.caller_ids = {}, [], {}, {}
        self.shelf_items, self.unmatched, self.projects = [], [], []
        self.path = tk.StringVar(); self.audience = tk.StringVar(value="Choose who reviewed this content")
        self.isolation = tk.StringVar(value="Choose a parsing option" if sys.platform == "win32" else "Required Linux sandbox")
        self.level = tk.StringVar(value="Getting started")
        self.search = tk.StringVar(); self.topic = tk.StringVar(value="Overview")
        self.scope = tk.StringVar(value="Around selection"); self.label = tk.StringVar()
        self.density = tk.StringVar(value="80 items"); self.show_static = tk.BooleanVar(value=True); self.show_inferred = tk.BooleanVar(value=True)
        self.map_status = tk.StringVar(value="Open a repository to explore the map."); self.zoom = 1.0
        self.pan_x, self.pan_y, self.drag_origin = 0.0, 0.0, None
        self.use_model = tk.BooleanVar(value=False); self.include_inferred = tk.BooleanVar(value=False); self.include_organization = tk.BooleanVar(value=False)
        self.goal = tk.StringVar(value="Understand a function"); self.prefer_unreviewed = tk.BooleanVar(value=False)
        self.callers_transitive = tk.BooleanVar(value=False)
        self.route_start = None; self.route_start_note = tk.StringVar(value="No path start selected.")
        self.status = tk.StringVar(value="Choose a local folder and who reviewed it.")
        root.title("Elucidary")
        root.geometry("1280x820"); root.minsize(820, 600)
        root.protocol("WM_DELETE_WINDOW", self.close)
        outer = ttk.Frame(root, padding=10); outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="Elucidary", font=("TkDefaultFont", 17, "bold")).pack(anchor="w")
        ttk.Label(outer, text="Local repository learning · source and labels may be unsuitable for children; adult review is required.", wraplength=1100).pack(anchor="w")
        open_row = ttk.Frame(outer); open_row.pack(fill="x", pady=4)
        ttk.Label(open_row, text="Repository folder").pack(side="left")
        self.path_entry = ttk.Entry(open_row, textvariable=self.path); self.path_entry.pack(side="left", fill="x", expand=True, padx=6)
        self.browse_button = ttk.Button(open_row, text="Browse…", command=self.browse); self.browse_button.pack(side="left")
        self.open_button = ttk.Button(open_row, text="Open repository", command=self.open_repository); self.open_button.pack(side="left", padx=4)
        self.rescan_button = ttk.Button(open_row, text="Rescan & compare", command=lambda: self.open_repository(compare=True)); self.rescan_button.pack(side="left")
        settings = ttk.Frame(outer); settings.pack(fill="x")
        ttk.Label(settings, text="Content review").pack(side="left")
        self.audience_box = ttk.Combobox(settings, textvariable=self.audience, state="readonly", width=31,
            values=("Choose who reviewed this content", "Adult", "Adult reviewed for a young learner", "Young learner without adult review"))
        self.audience_box.pack(side="left", padx=4); self.audience_box.bind("<<ComboboxSelected>>", self.audience_changed)
        ttk.Label(settings, text="Parsing").pack(side="left")
        parsing_options = (("Choose a parsing option", "Trusted repositories only: no parser sandbox") if sys.platform == "win32" else
                           ("Required Linux sandbox", "Trusted development: no Linux sandbox"))
        self.isolation_box = ttk.Combobox(settings, textvariable=self.isolation, state="readonly", width=44, values=parsing_options)
        self.isolation_box.pack(side="left", padx=4)
        ttk.Label(settings, text="Level").pack(side="left")
        level = ttk.Combobox(settings, textvariable=self.level, state="readonly", width=15, values=("Getting started", "Explore together", "Technical"))
        level.pack(side="left", padx=4); level.bind("<<ComboboxSelected>>", self.level_changed)
        self.include_vendored, self.include_generated = tk.BooleanVar(value=False), tk.BooleanVar(value=False)
        ttk.Checkbutton(settings, text="Include dependency and build folders", variable=self.include_vendored).pack(side="left", padx=(8, 0))
        ttk.Checkbutton(settings, text="Parse generated files", variable=self.include_generated).pack(side="left", padx=4)
        ttk.Label(outer, text="Adult review is self-attested, not age verification or filtering. Trusted development mode does not isolate hostile code.", wraplength=1100).pack(anchor="w")

        saved = ttk.LabelFrame(outer, text="Remembered folders · adult sessions only", padding=5); saved.pack(fill="x", pady=4)
        self.shelf_box = ttk.Combobox(saved, state="disabled", width=35); self.shelf_box.pack(side="left", fill="x", expand=True)
        self.refresh_shelf_button = ttk.Button(saved, text="Refresh list", command=self.refresh_shelf); self.refresh_shelf_button.pack(side="left", padx=3)
        self.use_shelf_button = ttk.Button(saved, text="Use folder", command=self.use_shelf); self.use_shelf_button.pack(side="left", padx=3)
        self.forget_button = ttk.Button(saved, text="Forget folder", command=self.forget); self.forget_button.pack(side="left", padx=3)
        self.remember_button = ttk.Button(saved, text="Remember current folder", command=self.remember); self.remember_button.pack(side="left", padx=3)
        ttk.Label(outer, textvariable=self.status, wraplength=1100).pack(fill="x")
        activity = ttk.Frame(outer); activity.pack(fill="x")
        ttk.Button(activity, text="Show scan warnings", command=self.show_warnings).pack(side="left")
        self.cancel_button = ttk.Button(activity, text="Stop", command=self.cancel_job, state="disabled"); self.cancel_button.pack(side="left", padx=4)
        self.progress_bar = ttk.Progressbar(activity, mode="determinate", maximum=100, length=220); self.progress_bar.pack(side="left", padx=4)
        self.progress_text = tk.StringVar(value="")
        ttk.Label(activity, textvariable=self.progress_text).pack(side="left", fill="x", expand=True)

        split = ttk.Panedwindow(outer, orient="horizontal"); split.pack(fill="both", expand=True, pady=5)
        left = ttk.Frame(split, padding=3); right = ttk.Frame(split, padding=3)
        split.add(left, weight=1); split.add(right, weight=1)
        controls = ttk.Frame(left); controls.pack(fill="x")
        ttk.Label(controls, text="Cosmic web").pack(side="left")
        scope = ttk.Combobox(controls, textvariable=self.scope, state="readonly", width=20, values=("Around selection", "This file", "Saved labels", "Repository overview"))
        scope.pack(side="left", padx=5); scope.bind("<<ComboboxSelected>>", lambda _: self.draw_graph())
        density = ttk.Combobox(controls, textvariable=self.density, state="readonly", width=9, values=("40 items", "80 items", "140 items"))
        density.pack(side="left"); density.bind("<<ComboboxSelected>>", lambda _: self.draw_graph())
        ttk.Button(controls, text="−", width=3, command=lambda: self.zoom_map(.8)).pack(side="left", padx=(5, 0))
        ttk.Button(controls, text="+", width=3, command=lambda: self.zoom_map(1.25)).pack(side="left")
        ttk.Button(controls, text="Reset", command=self.reset_zoom).pack(side="left", padx=3)
        ttk.Button(controls, text="Back", command=self.go_back).pack(side="left")
        edge_controls = ttk.Frame(left); edge_controls.pack(fill="x")
        ttk.Checkbutton(edge_controls, text="Source links", variable=self.show_static, command=self.draw_graph).pack(side="left")
        ttk.Checkbutton(edge_controls, text="Inferred links", variable=self.show_inferred, command=self.draw_graph).pack(side="left")
        ttk.Label(edge_controls, text="Pan:").pack(side="left", padx=(8, 0))
        for label, dx, dy in (("←", .15, 0), ("↑", 0, .15), ("↓", 0, -.15), ("→", -.15, 0)):
            ttk.Button(edge_controls, text=label, width=3, command=lambda x=dx, y=dy: self.pan_map(x, y)).pack(side="left")
        # The map and the item browser share the left side; the divider between them can be dragged.
        left_split = ttk.Panedwindow(left, orient="vertical"); left_split.pack(fill="both", expand=True)
        map_pane, browse_pane = ttk.Frame(left_split), ttk.Frame(left_split)
        left_split.add(map_pane, weight=3); left_split.add(browse_pane, weight=2)
        self.canvas = tk.Canvas(map_pane, background="#101629", height=230, highlightthickness=1, takefocus=True)
        self.canvas.pack(fill="both", expand=True); self.canvas.bind("<Configure>", lambda _: self.draw_graph())
        self.canvas.bind("<ButtonPress-1>", self.begin_pan); self.canvas.bind("<B1-Motion>", self.drag_pan)
        self.canvas.bind("<ButtonRelease-1>", lambda _: setattr(self, "drag_origin", None))
        for key, dx, dy in (("Left", .15, 0), ("Up", 0, .15), ("Down", 0, -.15), ("Right", -.15, 0)):
            self.canvas.bind("<" + key + ">", lambda _, x=dx, y=dy: self.pan_map(x, y))
        ttk.Label(map_pane, text="Solid: source facts · dashed gold: inferred · arrows: recorded direction · highlighted: path. An orange ring marks a label needing review. Drag empty space or use pan controls; the item and Links lists support keyboard navigation.", wraplength=620).pack(anchor="w")
        ttk.Label(map_pane, textvariable=self.map_status, wraplength=620).pack(anchor="w")
        find = ttk.Frame(browse_pane); find.pack(fill="x", pady=(4, 0))
        ttk.Label(find, text="Find item").pack(side="left")
        ttk.Entry(find, textvariable=self.search).pack(side="left", fill="x", expand=True, padx=4)
        self.search.trace_add("write", self.search_changed)
        self.tree_note = tk.StringVar(value="")
        ttk.Label(browse_pane, textvariable=self.tree_note, wraplength=620).pack(anchor="w")
        browser = ttk.Frame(browse_pane); browser.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(browser, columns=("kind", "path"), show="tree headings", height=8, selectmode="browse")
        self.tree.heading("#0", text="Name or label"); self.tree.heading("kind", text="Type"); self.tree.heading("path", text="Source")
        self.tree.column("#0", width=220); self.tree.column("kind", width=85); self.tree.column("path", width=150)
        tree_scroll = ttk.Scrollbar(browser, orient="vertical", command=self.tree.yview); self.tree.configure(yscrollcommand=tree_scroll.set)
        tree_scroll.pack(side="right", fill="y"); self.tree.pack(side="left", fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", self.tree_selected); self.tree.bind("<<TreeviewOpen>>", self.tree_opened)

        tabs = ttk.Notebook(right); tabs.pack(fill="both", expand=True); self.tabs = tabs
        learn, source, connections, practice, organize, guided, calls_tab, changes, coverage, finder = [ttk.Frame(tabs, padding=6) for _ in range(10)]
        self.learn_tab, self.source_tab, self.connections_tab, self.calls_tab, self.changes_tab, self.guided_tab = learn, source, connections, calls_tab, changes, guided
        self.coverage_tab, self.finder_tab = coverage, finder
        for frame, title in ((learn, "Learn"), (source, "Source"), (connections, "Links"), (practice, "Practice"), (organize, "Labels"), (guided, "Guide"), (calls_tab, "Calls"), (changes, "Changes"), (coverage, "Coverage"), (finder, "Search")):
            tabs.add(frame, text=title)
        ttk.Label(coverage, text="What this scan read, skipped and limited. An item missing from the map may simply not have been scanned.", wraplength=560).pack(anchor="w")
        ttk.Button(coverage, text="Clear local scan cache", command=self.clear_cache).pack(anchor="w")
        self.coverage_text = self.text_area(coverage, height=20)
        ttk.Label(finder, text="Find text in the scanned files. Only files whose bytes still match the scan are searched; a match is text, not a relationship.", wraplength=560).pack(anchor="w")
        finder_row = ttk.Frame(finder); finder_row.pack(fill="x")
        self.source_query = tk.StringVar()
        finder_entry = ttk.Entry(finder_row, textvariable=self.source_query); finder_entry.pack(side="left", fill="x", expand=True)
        finder_entry.bind("<Return>", lambda _: self.search_source())
        ttk.Button(finder_row, text="Search source", command=self.search_source).pack(side="left", padx=4)
        self.match_summary = tk.StringVar(value="Type at least two characters.")
        ttk.Label(finder, textvariable=self.match_summary, wraplength=560).pack(anchor="w")
        self.match_list = ttk.Treeview(finder, columns=("line", "text"), show="tree headings", height=14, selectmode="browse")
        self.match_list.heading("#0", text="File"); self.match_list.heading("line", text="Line"); self.match_list.heading("text", text="Text")
        self.match_list.column("#0", width=200); self.match_list.column("line", width=55); self.match_list.column("text", width=300)
        self.match_list.pack(fill="both", expand=True)
        self.match_list.bind("<Double-1>", lambda _: self.open_match()); self.match_list.bind("<Return>", lambda _: self.open_match())
        ttk.Button(finder, text="Open match in source", command=self.open_match).pack(anchor="w")
        learn_controls = ttk.Frame(learn); learn_controls.pack(fill="x")
        ttk.Label(learn_controls, text="Explain").pack(side="left")
        topics = ttk.Combobox(learn_controls, textvariable=self.topic, state="readonly", width=16,
            values=("Overview", "Inputs", "Returns", "Structure", "Calls", "Connections")); topics.pack(side="left", padx=4)
        ttk.Button(learn_controls, text="Show explanation", command=self.explain).pack(side="left")
        self.model_check = ttk.Checkbutton(learn, text="Also ask configured isolated local model (adult only; may be wrong)", variable=self.use_model)
        self.model_check.pack(anchor="w")
        ttk.Button(learn, text="Configure isolated local model…", command=self.configure_model).pack(anchor="w")
        self.learn_text = self.text_area(learn, height=12)
        call_tools = ttk.Notebook(calls_tab); call_tools.pack(fill="both", expand=True)
        written_calls, incoming_calls = ttk.Frame(call_tools, padding=3), ttk.Frame(call_tools, padding=3)
        call_tools.add(written_calls, text="Written here"); call_tools.add(incoming_calls, text="Possible callers")
        ttk.Label(written_calls, text="Select a written call to inspect it or open a possible target.", wraplength=520).pack(anchor="w")
        self.calls = tk.Listbox(written_calls, height=8, exportselection=False); self.calls.pack(fill="both", expand=True)
        self.call_detail = tk.StringVar(value="Select a written call to inspect what the scan knows.")
        self.calls.bind("<<ListboxSelect>>", lambda _: self.show_call_details())
        ttk.Label(written_calls, textvariable=self.call_detail, wraplength=520).pack(anchor="w")
        call_buttons = ttk.Frame(written_calls); call_buttons.pack(fill="x")
        ttk.Button(call_buttons, text="Compare call and target", command=self.compare_call).pack(anchor="w")
        ttk.Button(call_buttons, text="Open call in map", command=lambda: self.follow_call("map_node_id")).pack(anchor="w")
        ttk.Button(call_buttons, text="Open possible target", command=lambda: self.follow_call("target_id")).pack(anchor="w")
        self.caller_summary = tk.StringVar(value="Select a function to explore possible callers.")
        ttk.Label(incoming_calls, textvariable=self.caller_summary, wraplength=520).pack(anchor="w")
        ttk.Checkbutton(incoming_calls, text="Include possible callers up to three steps away", variable=self.callers_transitive,
                        command=self.refresh_callers).pack(anchor="w")
        self.caller_list = ttk.Treeview(incoming_calls, columns=("hops", "source"), show="tree headings", height=5, selectmode="browse")
        self.caller_list.heading("#0", text="Possible caller"); self.caller_list.heading("hops", text="Steps"); self.caller_list.heading("source", text="Call source")
        self.caller_list.column("#0", width=155); self.caller_list.column("hops", width=55); self.caller_list.column("source", width=170)
        self.caller_list.pack(fill="both", expand=True); self.caller_list.bind("<<TreeviewSelect>>", lambda _: self.show_caller())
        self.caller_list.bind("<Double-1>", lambda _: self.open_caller())
        self.caller_list.bind("<Return>", lambda _: self.open_caller())
        caller_buttons = ttk.Frame(incoming_calls); caller_buttons.pack(fill="x")
        ttk.Button(caller_buttons, text="Open caller", command=self.open_caller).pack(side="left")
        ttk.Button(caller_buttons, text="Open call source", command=self.open_caller_source).pack(side="left", padx=4)
        ttk.Button(caller_buttons, text="Find path in map", command=self.route_from_caller).pack(side="left")
        self.caller_text = self.text_area(incoming_calls, height=7)
        self.review_note = ttk.Label(learn, text="Review marks are optional, local, and not scores.", wraplength=560); self.review_note.pack(anchor="w")
        ttk.Button(learn, text="Toggle my review mark", command=self.toggle_review).pack(anchor="w")

        ttk.Label(source, text="The containing file. Its full text is shown only while its bytes still match the scan; otherwise the excerpt captured by the scan is shown. Code is unfiltered.", wraplength=560).pack(anchor="w")
        self.source_text = self.text_area(source, height=18)
        ttk.Label(source, text="Graph items in this file (including beyond the preview)").pack(anchor="w")
        self.source_list = ttk.Treeview(source, columns=("line", "type"), show="tree headings", height=7, selectmode="browse")
        self.source_list.heading("#0", text="Item"); self.source_list.heading("line", text="Line"); self.source_list.heading("type", text="Type")
        self.source_list.column("#0", width=220); self.source_list.column("line", width=60); self.source_list.column("type", width=90)
        self.source_list.pack(fill="both", expand=True); self.source_list.bind("<Double-1>", lambda _: self.open_source_item())
        self.source_list.bind("<Return>", lambda _: self.open_source_item())
        ttk.Button(source, text="Open selected source item in map", command=self.open_source_item).pack(anchor="w")

        ttk.Label(connections, text="Select a link to inspect its direction and source. Possible targets are inferred; none are observed runtime calls.", wraplength=560).pack(anchor="w")
        self.connection_list = ttk.Treeview(connections, columns=("direction", "kind", "evidence"), show="tree headings", height=8, selectmode="browse")
        self.connection_list.heading("#0", text="Other item"); self.connection_list.heading("direction", text="Direction")
        self.connection_list.heading("kind", text="Relationship"); self.connection_list.heading("evidence", text="Evidence")
        self.connection_list.column("#0", width=150); self.connection_list.column("direction", width=75)
        self.connection_list.column("kind", width=125); self.connection_list.column("evidence", width=75)
        self.connection_list.pack(fill="both", expand=True)
        self.connection_list.bind("<<TreeviewSelect>>", lambda _: self.show_connection())
        self.connection_list.bind("<Double-1>", lambda _: self.open_connection())
        self.connection_list.bind("<Return>", lambda _: self.open_connection())
        ttk.Button(connections, text="Open connected item", command=self.open_connection).pack(anchor="w")
        ttk.Button(connections, text="Open this link's source", command=self.open_connection_source).pack(anchor="w")
        self.connection_text = self.text_area(connections, height=9)

        practice_tools = ttk.Notebook(practice); practice_tools.pack(fill="both", expand=True)
        evidence_practice, reading_practice = ttk.Frame(practice_tools, padding=3), ttk.Frame(practice_tools, padding=3)
        practice_tools.add(evidence_practice, text="Evidence and execution"); practice_tools.add(reading_practice, text="Read the source")
        self.practice_prompt = tk.StringVar(value="Select a source item to find a question.")
        ttk.Label(evidence_practice, textvariable=self.practice_prompt, wraplength=560).pack(anchor="w")
        self.practice_source = ttk.Button(evidence_practice, text="", command=lambda: self.answer_practice("source")); self.practice_source.pack(anchor="w", pady=3)
        self.practice_ran = ttk.Button(evidence_practice, text="This definitely ran", command=lambda: self.answer_practice("ran")); self.practice_ran.pack(anchor="w", pady=3)
        ttk.Label(evidence_practice, text="Your answers are not saved and are not a score.", wraplength=560).pack(anchor="w")
        self.practice_text = self.text_area(evidence_practice, height=8)

        self.reading_questions, self.reading_index = [], 0
        self.reading_prompt = tk.StringVar(value="Select a function with captured source to find a reading question.")
        self.reading_source_note = tk.StringVar(value="")
        self.reading_choice = tk.IntVar(value=-1); self.reading_choices = [tk.StringVar(), tk.StringVar()]
        self.reading_option_buttons = []
        ttk.Label(reading_practice, textvariable=self.reading_prompt, wraplength=520).pack(anchor="w")
        ttk.Label(reading_practice, textvariable=self.reading_source_note, wraplength=520).pack(anchor="w")
        for index, variable in enumerate(self.reading_choices):
            option = tk.Radiobutton(reading_practice, textvariable=variable, variable=self.reading_choice, value=index,
                                    wraplength=500, justify="left", anchor="w")
            option.pack(fill="x"); self.reading_option_buttons.append(option)
        reading_buttons = ttk.Frame(reading_practice); reading_buttons.pack(fill="x")
        self.reading_check = ttk.Button(reading_buttons, text="Check answer", command=self.check_reading_answer); self.reading_check.pack(side="left")
        self.reading_next = ttk.Button(reading_buttons, text="Next question", command=self.next_reading_question); self.reading_next.pack(side="left", padx=4)
        self.reading_open = ttk.Button(reading_buttons, text="Open source", command=self.open_reading_source); self.reading_open.pack(side="left")
        ttk.Label(reading_practice, text="Open-book practice: answers are not saved and are not a score.", wraplength=520).pack(anchor="w")
        self.reading_feedback = self.text_area(reading_practice, height=7)

        self.label_tools = ttk.Notebook(organize); self.label_tools.pack(fill="both", expand=True)
        manage_labels, self.repair_labels = ttk.Frame(self.label_tools, padding=3), ttk.Frame(self.label_tools, padding=3)
        self.label_tools.add(manage_labels, text="Saved labels"); self.label_tools.add(self.repair_labels, text="Repair and reconnect")
        ttk.Label(manage_labels, text="Your label for the selected item").pack(anchor="w")
        ttk.Entry(manage_labels, textvariable=self.label).pack(fill="x")
        label_buttons = ttk.Frame(manage_labels); label_buttons.pack(fill="x", pady=3)
        ttk.Button(label_buttons, text="Save label locally", command=self.save_label).pack(side="left")
        ttk.Button(label_buttons, text="Remove label", command=self.remove_label).pack(side="left", padx=4)
        self.label_note = ttk.Label(manage_labels, text="Labels are stored in the local workspace.", wraplength=560); self.label_note.pack(anchor="w")
        ttk.Label(manage_labels, text="Saved labels in this repository").pack(anchor="w", pady=(6, 0))
        self.label_inventory = ttk.Treeview(manage_labels, columns=("state", "path"), show="tree headings", height=4, selectmode="browse")
        self.label_inventory.heading("#0", text="Label"); self.label_inventory.heading("state", text="Status"); self.label_inventory.heading("path", text="Source")
        self.label_inventory.column("#0", width=150); self.label_inventory.column("state", width=115); self.label_inventory.column("path", width=170)
        self.label_inventory.pack(fill="x"); self.label_inventory.bind("<Double-1>", lambda _: self.open_inventory_label())
        self.label_inventory.bind("<Return>", lambda _: self.open_inventory_label())
        ttk.Button(manage_labels, text="Open selected label", command=self.open_inventory_label).pack(anchor="w")
        ttk.Label(self.repair_labels, text="Attach an unmatched saved label to the selected item").pack(anchor="w", pady=(10, 0))
        self.unmatched_box = ttk.Combobox(self.repair_labels, state="readonly"); self.unmatched_box.pack(fill="x")
        ttk.Button(self.repair_labels, text="Attach selected label here", command=self.rebind_label).pack(anchor="w", pady=3)
        ttk.Label(self.repair_labels, text="Moved repository: connect a saved label collection to this folder (adult only)", wraplength=560).pack(anchor="w", pady=(10, 0))
        self.project_box = ttk.Combobox(self.repair_labels, state="readonly"); self.project_box.pack(fill="x")
        ttk.Button(self.repair_labels, text="Connect selected collection", command=self.reconnect).pack(anchor="w", pady=3)

        ttk.Label(guided, text="Start from the selected item. A path is not a runtime trace.", wraplength=560).pack(anchor="w")
        guide_tools = ttk.Notebook(guided); guide_tools.pack(fill="x")
        lessons, routes = ttk.Frame(guide_tools, padding=3), ttk.Frame(guide_tools, padding=3)
        guide_tools.add(lessons, text="Learning goals"); guide_tools.add(routes, text="Between items")
        ttk.Combobox(lessons, textvariable=self.goal, state="readonly", values=("Understand a function", "Explore written calls", "Explore a dependency")).pack(fill="x")
        ttk.Checkbutton(lessons, text="Prefer items I have not marked reviewed", variable=self.prefer_unreviewed).pack(anchor="w")
        ttk.Button(lessons, text="Build guided path", command=self.build_guide).pack(anchor="w", pady=4)
        ttk.Checkbutton(routes, text="Include inferred connections", variable=self.include_inferred).pack(anchor="w")
        ttk.Checkbutton(routes, text="Include file and project containment", variable=self.include_organization).pack(anchor="w")
        ttk.Label(routes, text="Select a start and destination in the map or item list.", wraplength=560).pack(anchor="w")
        route_buttons = ttk.Frame(routes); route_buttons.pack(fill="x")
        ttk.Button(route_buttons, text="Set selected item as start", command=self.set_route_start).pack(side="left")
        ttk.Button(route_buttons, text="Find path to selection", command=self.build_item_path).pack(side="left", padx=4)
        ttk.Label(routes, textvariable=self.route_start_note, wraplength=560).pack(anchor="w")
        label_route = ttk.Frame(routes); label_route.pack(fill="x")
        self.destination = ttk.Combobox(label_route, state="readonly"); self.destination.pack(side="left", fill="x", expand=True)
        ttk.Button(label_route, text="Path to label", command=self.build_label_path).pack(side="left", padx=4)
        self.steps = tk.Listbox(guided, height=5, exportselection=False); self.steps.pack(fill="x")
        self.steps.bind("<<ListboxSelect>>", lambda _: self.show_step())
        step_controls = ttk.Frame(guided); step_controls.pack(fill="x")
        ttk.Button(step_controls, text="Previous step", command=lambda: self.move_step(-1)).pack(side="left")
        ttk.Button(step_controls, text="Next step", command=lambda: self.move_step(1)).pack(side="left", padx=4)
        ttk.Button(step_controls, text="Open step source", command=self.open_step_source).pack(side="left", padx=4)
        self.step_text = self.text_area(guided, height=10)
        self.change_summary = tk.StringVar(value="Use Rescan & compare to compare this session's snapshots.")
        ttk.Label(changes, textvariable=self.change_summary, wraplength=520).pack(anchor="w")
        self.change_list = ttk.Treeview(changes, columns=("status", "path"), show="tree headings", height=7, selectmode="browse")
        self.change_list.heading("#0", text="Item"); self.change_list.heading("status", text="Change"); self.change_list.heading("path", text="Source")
        self.change_list.column("#0", width=170); self.change_list.column("status", width=90); self.change_list.column("path", width=170)
        self.change_list.pack(fill="both", expand=True)
        self.change_list.bind("<<TreeviewSelect>>", lambda _: self.show_change())
        self.change_list.bind("<Double-1>", lambda _: self.open_changed_item())
        self.change_list.bind("<Return>", lambda _: self.open_changed_item())
        ttk.Button(changes, text="Open current item", command=self.open_changed_item).pack(anchor="w")
        self.change_text = self.text_area(changes, height=8)
        self.path.trace_add("write", self.path_changed)
        self.audience_changed()
        root.after(80, self.poll_jobs)

    def text_area(self, parent, height):
        frame = self.ttk.Frame(parent); frame.pack(fill="both", expand=True)
        text = self.tk.Text(frame, height=height, width=40, wrap="word", state="disabled", font="TkFixedFont")
        scroll = self.ttk.Scrollbar(frame, orient="vertical", command=text.yview); text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y"); text.pack(side="left", fill="both", expand=True)
        return text

    @staticmethod
    def write(text, value):
        text.configure(state="normal"); text.delete("1.0", "end"); text.insert("1.0", value); text.configure(state="disabled")

    def level_key(self):
        return {"Getting started": "beginner", "Explore together": "explore", "Technical": "technical"}[self.level.get()]

    def level_changed(self, *_):
        if self.busy or not self.controller.data: return
        selected = self.connection_list.selection()
        edge = self.connection_ids[selected[0]][0] if selected and selected[0] in self.connection_ids else None
        call_selection = self.calls.curselection()
        caller = self.selected_caller()
        caller_key = (caller["caller_id"], tuple((x["source"], x["target"], x.get("path"), x.get("line"), x.get("written")) for x in caller["path"])) if caller else None
        self.show_selected()
        if call_selection and call_selection[0] < self.calls.size():
            self.calls.selection_set(call_selection[0]); self.show_call_details()
        if edge is not None:
            self.prepare_connections(self.controller.nodes[self.controller.selected], focus=edge)
            item = next((key for key, value in self.connection_ids.items() if value[0] is edge), None)
            if item is not None:
                self.connection_list.selection_set(item); self.connection_list.see(item); self.show_connection()
        if caller_key is not None:
            item = next((key for key, row in self.caller_ids.items() if
                         (row["caller_id"], tuple((x["source"], x["target"], x.get("path"), x.get("line"), x.get("written")) for x in row["path"])) == caller_key), None)
            if item is not None:
                self.caller_list.selection_set(item); self.caller_list.see(item); self.show_caller()

    def audience_key(self):
        return {"Adult": "adult", "Adult reviewed for a young learner": "adult_reviewed"}.get(self.audience.get())

    def isolation_key(self):
        return {"Required Linux sandbox": "required", "Trusted development: no Linux sandbox": "portable",
                "Trusted repositories only: no parser sandbox": "portable"}.get(self.isolation.get())

    def notify_error(self, exc):
        from tkinter import messagebox
        self.status.set(str(exc)); messagebox.showerror("Elucidary", str(exc), parent=self.root)

    def run_job(self, work, done, message, cancellable=False):
        if self.busy:
            return
        self.busy = True; self.open_button.configure(state="disabled"); self.rescan_button.configure(state="disabled"); self.status.set(message)
        self.audience_box.configure(state="disabled"); self.isolation_box.configure(state="disabled"); self.browse_button.configure(state="disabled"); self.path_entry.configure(state="disabled")
        self.cancel_button.configure(state="normal" if cancellable and self.cancel_event is not None else "disabled")
        self.progress_bar.configure(mode="indeterminate"); self.progress_bar.start(60); self.progress_text.set("")
        def run():
            try: self.jobs.put((done, work(), None))
            except Exception as exc: self.jobs.put((done, None, exc))
        threading.Thread(target=run, daemon=True).start()

    def report_progress(self, stage, done, total, detail):
        """Called from the working thread; the window reads the newest report on its next poll."""
        self.progress_reports.put((stage, done, total, detail))

    def cancel_job(self):
        if self.busy and self.cancel_event is not None:
            self.cancel_event.set(); self.cancel_button.configure(state="disabled")
            self.status.set("Stopping… the current files are finishing.")

    def show_progress(self):
        report = None
        try:
            while True: report = self.progress_reports.get_nowait()
        except queue.Empty:
            pass
        if report is None or not self.busy: return
        stage, done, total, detail = report
        if total:
            if str(self.progress_bar.cget("mode")) != "determinate":
                self.progress_bar.stop(); self.progress_bar.configure(mode="determinate")
            self.progress_bar.configure(value=100 * done / max(total, 1))
            self.progress_text.set(f"{stage}: {done:,} of {total:,}" + (f" · {detail[-60:]}" if detail else ""))
        else:
            self.progress_text.set(f"{stage}: {done:,} source files found" + (f" · {detail[-60:]}" if detail else ""))

    def poll_jobs(self):
        self.show_progress()
        try:
            done, result, error = self.jobs.get_nowait()
        except queue.Empty:
            pass
        else:
            self.busy = False; self.open_button.configure(state="normal"); self.rescan_button.configure(state="normal")
            self.audience_box.configure(state="readonly"); self.isolation_box.configure(state="readonly"); self.browse_button.configure(state="normal"); self.path_entry.configure(state="normal")
            self.cancel_button.configure(state="disabled"); self.cancel_event = None
            self.progress_bar.stop(); self.progress_bar.configure(mode="determinate", value=0); self.progress_text.set("")
            while not self.progress_reports.empty():
                try: self.progress_reports.get_nowait()
                except queue.Empty: break
            if self.closing:
                self.controller.service.close(); self.root.destroy(); return
            if error and getattr(error, "code", None) == "scan_cancelled":
                self.status.set("Stopped. Nothing from that scan was kept; open the folder again when ready.")
            elif error: self.notify_error(error)
            else:
                try: done(result)
                except Exception as exc: self.notify_error(exc)
        self.root.after(80, self.poll_jobs)

    def close(self):
        if self.busy:
            self.closing = True
            if self.cancel_event is not None: self.cancel_event.set()
            self.root.withdraw(); return
        self.controller.service.close(); self.root.destroy()

    def browse(self):
        from tkinter import filedialog
        if not self.busy:
            folder = filedialog.askdirectory(parent=self.root, mustexist=True)
            if folder: self.path.set(folder)

    def clear_view(self):
        self.controller.clear(); self.tree.delete(*self.tree.get_children()); self.source_list.delete(*self.source_list.get_children())
        self.connection_list.delete(*self.connection_list.get_children()); self.connection_ids = {}; self.history = []
        self.caller_list.delete(*self.caller_list.get_children()); self.caller_ids = {}
        self.caller_summary.set("Select a function to explore possible callers.")
        self.change_list.delete(*self.change_list.get_children()); self.change_ids = {}
        self.change_summary.set("Use Rescan & compare to compare this session's snapshots.")
        self.canvas.delete("all"); self.calls.delete(0, "end"); self.call_detail.set("Select a written call to inspect what the scan knows."); self.steps.delete(0, "end")
        self.tree_ids, self.tree_rows, self.tree_folder_rows, self.tree_pending = {}, {}, {}, {}
        self.tree_note.set(""); self.source_shown, self.overview_text, self.labeled_nodes, self.graph_cache = None, "", [], None
        self.match_list.delete(*self.match_list.get_children()); self.match_ids = {}; self.match_summary.set("Type at least two characters.")
        for text in (self.learn_text, self.source_text, self.connection_text, self.practice_text, self.step_text, self.change_text, self.caller_text, self.coverage_text): self.write(text, "")
        self.practice_prompt.set("Select a source item to find a question."); self.practice_source.configure(state="disabled"); self.practice_ran.configure(state="disabled")
        self.reset_reading_practice()
        self.map_status.set("Open a repository to explore the map."); self.zoom = 1.0; self.pan_x = self.pan_y = 0.0; self.drag_origin = None
        self.label.set(""); self.use_model.set(False); self.model_check.configure(state="disabled")
        self.label_inventory.delete(*self.label_inventory.get_children()); self.label_inventory_ids = {}
        self.route_start = None; self.route_start_note.set("No path start selected.")
        self.unmatched_box.configure(values=()); self.project_box.configure(values=()); self.destination.configure(values=())

    def path_changed(self, *_):
        if self.controller.data and self.path.get() != getattr(self, "opened_path", ""):
            self.clear_view(); self.status.set("Folder changed; open it to scan again.")

    def audience_changed(self, *_):
        if self.busy: return
        self.clear_view(); self.shelf_items = []; self.shelf_box.set(""); self.shelf_box.configure(values=(), state="disabled")
        adult = self.audience_key() == "adult"
        for button in (self.refresh_shelf_button, self.use_shelf_button, self.forget_button, self.remember_button):
            button.configure(state="normal" if adult else "disabled")
        self.status.set("Review choice is self-attested. Open a folder to continue." if self.audience_key() else "Adult review is required before opening a repository.")

    def open_repository(self, compare=False):
        if self.busy: return
        audience, path, isolation = self.audience_key(), self.path.get().strip(), self.isolation_key()
        if not audience or not path:
            self.status.set("Choose a folder and an adult review option first."); return
        if not isolation:
            self.status.set("Choose a parsing option. Windows currently supports trusted repositories only."); return
        baseline = None
        if compare:
            if not self.controller.data or self.controller.audience != audience or path != getattr(self, "opened_path", ""):
                self.status.set("Open this folder with the same review choice before comparing scans."); return
            from .repository_changes import baseline_of
            baseline = (baseline_of(self.controller.data), self.controller.audience)
        self.clear_view()
        cancel = self.cancel_event = threading.Event()
        vendored, generated = self.include_vendored.get(), self.include_generated.get()
        self.run_job(lambda: self.controller.open(path, audience, isolation, baseline, progress=self.report_progress, should_cancel=cancel.is_set,
                                                  include_vendored=vendored, include_generated=generated),
                     lambda result: self.loaded(result, path), "Reading source without executing repository code…", cancellable=True)

    def loaded(self, result, path):
        self.opened_path = path
        coverage = result.get("coverage") or {}
        found = coverage.get("supported_files_found")
        reach = "" if found is None else f" of {'at least ' if coverage.get('supported_files_found_is_lower_bound') else ''}{found:,} supported files found"
        self.status.set(f"Inspected {result['files_inspected']:,} files{reach}; {len(result['warnings'])} scan warnings. " +
                        ("Trusted development mode is not a hostile-code sandbox." if result["parser_isolation"] == "portable" else "Required parser isolation selected."))
        self.model_check.configure(state="normal" if result.get("local_assistant_available") else "disabled")
        self.projects = result.get("saved_projects", []) if self.controller.audience == "adult" else []
        self.project_box.configure(values=[f"{p['name']} · {p['labels']} labels" for p in self.projects]); self.project_box.set("")
        self.overview_text = format_repository_overview(result)
        self.labeled_nodes = [node for node in self.controller.nodes.values() if node.get("label")]
        self.show_coverage()
        self.refresh_tree(); self.refresh_destinations(); self.select("repo")
        self.refresh_changes(result.get("comparison"))
        if result.get("comparison") is not None: self.tabs.select(self.changes_tab)
        if result.get("comparison_unavailable"):
            self.status.set("The folder, saved collection, or review choice changed; the previous snapshot was not compared.")
        if result["warnings"]:
            self.write(self.learn_text, "Scan limits and omissions:\n" + "\n".join(result["warnings"]) + "\n\nSelect an item to learn more.")

    def show_coverage(self):
        if not self.controller.data: return
        try: cache = self.controller.service.repository_cache_status()
        except Exception: cache = None
        self.write(self.coverage_text, format_coverage(self.controller.data, cache))

    def clear_cache(self):
        if self.busy: return
        try:
            self.controller.service.repository_cache_clear(); self.show_coverage()
            self.status.set("Local scan cache cleared. Labels, review marks and remembered folders were kept; the next scan parses every file again.")
        except Exception as exc: self.notify_error(exc)

    # The browser shows folders while the search box is empty and matches while it is not.
    TREE_CHILD_LIMIT, TREE_SEARCH_LIMIT = 2000, 500

    def search_changed(self, *_):
        if self.search_after is not None:
            try: self.root.after_cancel(self.search_after)
            except Exception: pass
        self.search_after = self.root.after(220, self.apply_search)

    def apply_search(self):
        self.search_after = None
        self.refresh_tree()

    def tree_label(self, node, flat):
        if node.get("label"): return node["label"]
        name = str(node.get("name") or node["kind"])
        return name.rsplit("/", 1)[-1] if node["kind"] == "file" and not flat else name

    def tree_insert(self, parent, ident, flat=False):
        node = self.controller.nodes[ident]
        item = self.tree.insert(parent, "end", text=self.tree_label(node, flat), values=(node["kind"], node.get("path") or ""))
        self.tree_ids[item] = ident; self.tree_rows[ident] = item
        if not flat:
            index = self.controller.index
            if node["kind"] == "file" and index.in_file(node["path"]):
                self.tree_pending[item] = ("file", node["path"]); self.tree.insert(item, "end", text="…")
            elif node["kind"] == "function" and any(call.get("map_node_id") for call in node.get("call_sites", [])):
                self.tree_pending[item] = ("function", ident); self.tree.insert(item, "end", text="…")
        return item

    def tree_fill(self, item):
        """Add a row's children the first time it is opened."""
        pending = self.tree_pending.pop(item, None)
        if pending is None: return
        self.tree.delete(*self.tree.get_children(item))
        kind, value = pending; index = self.controller.index
        if kind == "folder":
            self.tree_fill_folder(item, value)
        elif kind == "file":
            members = index.file_items(value)
            for node in members[:self.TREE_CHILD_LIMIT]: self.tree_insert(item, node["id"])
            if len(members) > self.TREE_CHILD_LIMIT:
                self.tree.insert(item, "end", text=f"… {len(members) - self.TREE_CHILD_LIMIT} more items; use Find item")
        else:
            for call in self.controller.nodes[value].get("call_sites", []):
                if call.get("map_node_id") in self.controller.nodes and call["map_node_id"] not in self.tree_rows:
                    self.tree_insert(item, call["map_node_id"])

    def tree_fill_folder(self, parent, folder):
        folders, files = self.controller.index.folders().get(folder, ((), ()))
        shown = 0
        for name in folders:
            if shown >= self.TREE_CHILD_LIMIT: break
            child = name if not folder else folder + "/" + name
            item = self.tree.insert(parent, "end", text=name + "/", values=("folder", child))
            self.tree_folder_rows[child] = item; self.tree_pending[item] = ("folder", child); self.tree.insert(item, "end", text="…")
            shown += 1
        for ident in files:
            if shown >= self.TREE_CHILD_LIMIT: break
            self.tree_insert(parent, ident); shown += 1
        if len(folders) + len(files) > shown:
            self.tree.insert(parent, "end", text=f"… {len(folders) + len(files) - shown} more entries; use Find item")

    def tree_opened(self, *_):
        if self.busy or not self.controller.data: return
        item = self.tree.focus()
        if item: self.tree_fill(item)

    def reveal(self, ident):
        """Show and highlight an item in the browser, opening its folders and file when needed."""
        if not ident or not self.controller.data: return
        if ident not in self.tree_rows and not self.search.get().strip():
            node = self.controller.nodes.get(ident) or {}
            path, folder = node.get("path"), ""
            if not path: return
            for part in path.split("/")[:-1]:
                folder = part if not folder else folder + "/" + part
                row = self.tree_folder_rows.get(folder)
                if row is None: return
                self.tree_fill(row); self.tree.item(row, open=True)
            file_row = self.tree_rows.get("file:" + path)
            if file_row is None: return
            if ident != "file:" + path:
                self.tree_fill(file_row); self.tree.item(file_row, open=True)
                owner = self.tree_rows.get(node.get("owner_id"))
                if ident not in self.tree_rows and owner is not None:
                    self.tree_fill(owner); self.tree.item(owner, open=True)
        row = self.tree_rows.get(ident)
        if row is None:
            self.tree.selection_remove(*self.tree.selection()); return
        if self.tree.selection() != (row,): self.tree.selection_set(row)
        self.tree.see(row)

    def update_tree_row(self, ident):
        row = self.tree_rows.get(ident)
        if row is not None and ident in self.controller.nodes:
            self.tree.item(row, text=self.tree_label(self.controller.nodes[ident], bool(self.search.get().strip())))
        self.labeled_nodes = [node for node in self.controller.nodes.values() if node.get("label")]
        self.graph_cache = None

    def refresh_tree(self):
        if self.busy: return
        self.tree.delete(*self.tree.get_children())
        self.tree_ids, self.tree_rows, self.tree_folder_rows, self.tree_pending = {}, {}, {}, {}
        index = self.controller.index
        if not self.controller.data or index is None:
            self.tree_note.set(""); return
        query = self.search.get().strip()
        if query:
            found, total = index.search(query, self.TREE_SEARCH_LIMIT)
            for ident in found: self.tree_insert("", ident, flat=True)
            self.tree_note.set(f"{total:,} items match by name, label, path or type" +
                               (f"; the first {len(found)} are shown. Type more to narrow the list." if total > len(found) else "."))
        else:
            self.tree_insert("", "repo"); self.tree_fill_folder("", "")
            self.tree_note.set(f"{len(self.controller.nodes):,} items by folder. Open a file to see its functions; type above to find by name.")
        self.reveal(self.controller.selected)

    def tree_selected(self, *_):
        if self.busy: return
        selection = self.tree.selection()
        if selection and selection[0] in self.tree_ids and self.tree_ids[selection[0]] != self.controller.selected:
            self.select(self.tree_ids[selection[0]])

    def select(self, ident, remember=True):
        if self.busy or not self.controller.data: return
        try:
            if remember and self.controller.selected and self.controller.selected != ident:
                self.history.append(self.controller.selected)
                self.history = self.history[-100:]
            self.controller.select(ident); self.reveal(ident); self.draw_graph(); self.show_selected()
        except ServiceError as exc: self.notify_error(exc)

    def go_back(self):
        if not self.busy and self.history: self.select(self.history.pop(), remember=False)
        else: self.status.set("No earlier item in this trail.")

    def zoom_map(self, factor):
        self.zoom = min(3.0, max(.7, self.zoom * factor)); self.draw_graph()

    def reset_zoom(self):
        self.zoom = 1.0; self.pan_x = self.pan_y = 0.0; self.draw_graph()

    def pan_map(self, dx, dy):
        if self.busy or not self.controller.data: return
        self.pan_x = min(2.0, max(-2.0, self.pan_x + dx))
        self.pan_y = min(2.0, max(-2.0, self.pan_y + dy))
        self.draw_graph()

    def begin_pan(self, event):
        if self.busy or not self.controller.data: return
        current = self.canvas.find_withtag("current")
        tags = self.canvas.gettags(current[0]) if current else ()
        self.drag_origin = None if any(tag in tags for tag in ("graph_edge", "graph_node", "graph_label", "graph_group")) else (event.x, event.y)

    def drag_pan(self, event):
        if self.drag_origin is None or self.busy or not self.controller.data: return
        x, y = self.drag_origin; self.drag_origin = (event.x, event.y)
        self.pan_map((event.x - x) / max(self.canvas.winfo_width(), 1), (event.y - y) / max(self.canvas.winfo_height(), 1))

    def draw_graph(self):
        canvas = self.canvas; canvas.delete("all")
        if not self.controller.data: return
        index, nodes_by_id = self.controller.index, self.controller.nodes
        shown = {name for name, variable in (("static", self.show_static), ("inferred", self.show_inferred)) if variable.get()}
        steps = (self.controller.route or {}).get("steps", [])
        route = [s["node_id"] for s in steps]
        scope = self.scope.get()
        focus_path = nodes_by_id[self.controller.selected].get("path") or None if scope == "This file" else None
        overview = scope == "Repository overview" or (scope == "This file" and not focus_path)
        limit = int(self.density.get().split()[0])
        # Panning and zooming reuse the chosen items; only a new selection, scope or filter recomputes them.
        key = (self.controller.data.get("scan_id"), self.controller.selected, tuple(route), limit, overview, focus_path, scope, tuple(sorted(shown)), len(self.labeled_nodes))
        if self.graph_cache is None or self.graph_cache[0] != key:
            chosen, points = graph_view(self.labeled_nodes, shown, self.controller.selected, route, limit=limit,
                                        overview=overview, focus_path=focus_path, labeled=scope == "Saved labels", index=index)
            inside = set(chosen)
            edges = [e for ident in chosen for e in index.incident(ident)
                     if e["source"] == ident and e["target"] in inside and e["evidence"] in shown]
            self.graph_cache = (key, chosen, points, edges)
        _, chosen, points, edges = self.graph_cache
        w, h = max(canvas.winfo_width(), 1), max(canvas.winfo_height(), 1)
        sx, sy = w / 900, h / 520
        center = points.get(self.controller.selected, (450, 260)) if self.zoom != 1 else (450, 260)
        projected = {ident: (w * (.5 + self.pan_x) + (x - center[0]) * sx * self.zoom,
                             h * (.5 + self.pan_y) + (y - center[1]) * sy * self.zoom) for ident, (x, y) in points.items()}
        for i in range(75):
            x, y = ((i * 197 + 29) % 900) * sx, ((i * 113 + 47) % 520) * sy
            canvas.create_oval(x, y, x+1, y+1, fill="#55617d", outline="")
        groups = graph_constellations(nodes_by_id, chosen, projected, self.controller.selected)
        for path, bounds, count in groups:
            left, top, right, bottom = bounds
            if right < 0 or bottom < 0 or left > w or top > h: continue
            if count > 1:
                canvas.create_oval(left, top, right, bottom, outline="#314364", dash=(2, 5))
            if w >= 300 and h >= 150 and right - left >= 48:
                title = path if len(path) <= 30 else "…" + path[-29:]
                title_id = canvas.create_text(max(4, left), max(12, top), anchor="sw", text=title,
                                              fill="#9cb4d7", font=("TkDefaultFont", 8), tags=("graph_group",))
                file_id = "file:" + path
                if file_id in nodes_by_id:
                    canvas.tag_bind(title_id, "<Button-1>", lambda _, value=file_id: self.select(value))
        route_edges = {(s["via"]["source"], s["via"]["target"], s["via"]["kind"], s["via"].get("path"), s["via"].get("line")) for s in steps if s.get("via")}
        for edge in edges:
            if edge["source"] not in projected or edge["target"] not in projected: continue
            a, b = projected[edge["source"]], projected[edge["target"]]
            highlight = (edge["source"], edge["target"], edge["kind"], edge.get("path"), edge.get("line")) in route_edges
            dx, dy = b[0] - a[0], b[1] - a[1]; distance = math.hypot(dx, dy)
            end = (b[0] - dx / distance * 8, b[1] - dy / distance * 8) if distance > 12 else b
            line = canvas.create_line(a[0], a[1], end[0], end[1], fill="#ffd777" if edge["evidence"] == "inferred" else "#d9f9eb" if highlight else "#667aa7", width=4 if highlight else 2,
                                      dash=(5, 4) if edge["evidence"] == "inferred" else (), arrow="last", tags=("graph_edge",))
            canvas.tag_bind(line, "<Button-1>", lambda _, value=edge: self.open_graph_connection(value))
        colors = {"repository": "#fff3b0", "file": "#8ed3ff", "function": "#d2b7ff", "dependency": "#f4c287", "callsite": "#ffb4db"}
        for ident in chosen:
            n = nodes_by_id[ident]; x, y = projected[ident]
            radius = 8 if ident == self.controller.selected else 5
            if ident == self.controller.selected or ident in route:
                canvas.create_oval(x-radius-4, y-radius-4, x+radius+4, y+radius+4, outline="#66abbd")
            if n.get("label") and n.get("label_needs_review"):
                canvas.create_oval(x-radius-7, y-radius-7, x+radius+7, y+radius+7, outline="#ffb35c", width=2)
            dot = canvas.create_oval(x-radius, y-radius, x+radius, y+radius, fill=colors.get(n["kind"], "white"), outline="white" if ident == self.controller.selected else "#101629", width=2, tags=("graph_node",))
            canvas.tag_bind(dot, "<Button-1>", lambda _, value=ident: self.select(value))
        for ident, text, x, y in graph_labels([nodes_by_id[i] for i in chosen], projected, self.controller.selected, route, w, h):
            label = canvas.create_text(x, y, anchor="w", text=text, fill="white", font=("TkDefaultFont", 9, "bold"), tags=("graph_label",))
            canvas.tag_bind(label, "<Button-1>", lambda _, value=ident: self.select(value))
        hidden_route = any(s["via"].get("evidence") not in shown for s in steps if s.get("via"))
        scope_note = ("Select a source item to focus its file; showing the overview. " if scope == "This file" and not focus_path else
                      ("Showing saved labels and nearby connections. " if self.labeled_nodes else
                       "No labels are saved yet; showing the selection and nearby connections. ") if scope == "Saved labels" else
                      "Showing this file and any active path. " if focus_path else "")
        self.map_status.set(scope_note + f"Showing {len(chosen)} of {len(nodes_by_id):,} scanned items; all are available in the list. File groupings are visual, not new relationships." + (" Some path links are hidden by the filters." if hidden_route else ""))

    def show_selected(self):
        if self.busy or not self.controller.data or self.controller.selected not in self.controller.nodes: return
        node = self.controller.nodes[self.controller.selected]
        self.label.set(node.get("label") or "")
        self.review_note.configure(text=("Marked reviewed, but source has changed; review it again." if node.get("review_needs_refresh") else "You marked this item reviewed locally." if node.get("reviewed") else "Not marked reviewed. A mark is optional and is not a score."))
        self.label_note.configure(text="Source changed since this label was saved; check that it still fits." if node.get("label_needs_review") else "Labels are stored in the local workspace.")
        self.unmatched = [x for x in self.controller.data.get("unmatched_labels", []) if not node.get("label") and (not x.get("kind") or x["kind"] == node["kind"])]
        self.unmatched_box.configure(values=[f"{x['label']} · {x.get('path') or x['node_id']}" + (" · matching content" if x.get("suggested_node_id") == node["id"] else "") for x in self.unmatched]); self.unmatched_box.set("")
        self.calls.delete(0, "end"); self.call_indices = []
        self.call_detail.set("Select a written call to inspect what the scan knows.")
        for index, call in enumerate(node.get("call_sites", [])[:30]):
            self.call_indices.append(index)
            self.calls.insert("end", f"{call['written']} · {call['path']}:{call['line']} · " + ("possible target (inferred)" if call.get("target_id") else "unresolved"))
        self.show_source(node)
        self.prepare_connections(node); self.prepare_practice(node); self.prepare_callers(node)
        try:
            overview = self.overview_text if node["kind"] == "repository" else ""
            self.write(self.learn_text, overview + format_explanation(self.controller.explain(self.topic.get().lower(), self.level_key())))
        except ServiceError as exc: self.write(self.learn_text, str(exc))

    def refresh_changes(self, comparison):
        self.change_list.delete(*self.change_list.get_children()); self.change_ids = {}
        if comparison is None:
            self.change_summary.set("Use Rescan & compare to compare this session's snapshots.")
            self.write(self.change_text, "")
            return
        counts = comparison["counts"]
        self.change_summary.set(f"Session only: {counts['changed']} changed, {counts['added']} added, {counts['removed']} removed, {counts['line moved']} with a moved line. " +
                                ("The list is limited to the first 100. " if comparison["truncated"] else "") +
                                "Only scanned files and functions are compared.")
        for change in comparison["changes"]:
            snapshot = change["after"] or change["before"]
            label = change["label"] or snapshot.get("name") or change["node_id"]
            item = self.change_list.insert("", "end", text=label, values=(change["status"], snapshot.get("path") or ""))
            self.change_ids[item] = change
        self.write(self.change_text, comparison["limitation"])

    def show_change(self):
        if self.busy or not self.controller.data: return
        selected = self.change_list.selection()
        if not selected or selected[0] not in self.change_ids: return
        change = self.change_ids[selected[0]]
        lines = [f"{change['status'].title()} {change['kind']} · {change['label'] or change['node_id']}"]
        for title, snapshot in (("Before", change["before"]), ("After", change["after"])):
            if snapshot is None:
                lines.append(f"\n{title}: not recorded in that scan."); continue
            lines.append(f"\n{title}: {snapshot.get('path') or 'unknown'}:{snapshot.get('line') or '?'} · SHA-256 {snapshot.get('sha256') or 'unavailable'}")
            lines.extend(f"{line['line']}: {line['text']}" + (" [line shortened]" if line.get("truncated") else "") for line in snapshot["excerpt"])
            if not snapshot["excerpt"]: lines.append("No excerpt was captured.")
        lines.append("\n" + self.controller.data["comparison"]["limitation"])
        self.write(self.change_text, "\n".join(lines))

    def open_changed_item(self):
        if self.busy or not self.controller.data: return
        selected = self.change_list.selection()
        if not selected or selected[0] not in self.change_ids:
            self.status.set("Choose a changed item first."); return
        change = self.change_ids[selected[0]]
        if change["after"] is None or change["node_id"] not in self.controller.nodes:
            self.status.set("This item was not recorded in the current scan; its earlier excerpt remains in Changes."); return
        self.select(change["node_id"])
        self.tabs.select(self.source_tab)
        self.status.set("Opened the current item in the map and source view.")

    def show_warnings(self):
        if self.busy or not self.controller.data:
            self.status.set("Open a repository to inspect scan warnings."); return
        warnings = self.controller.data.get("warnings", [])
        self.write(self.learn_text, "Scan warnings and omissions:\n" + ("\n".join(warnings) if warnings else "No warnings were recorded. This is not proof that the scan is complete."))
        self.status.set("Scan warnings are shown in the Learn tab.")

    def show_source(self, node, focus_line=None):
        file = self.controller.nodes.get("file:" + str(node.get("path") or ""))
        self.source_list.delete(*self.source_list.get_children()); self.source_ids = {}
        if not file:
            self.source_shown = None
            self.write(self.source_text, "Select a file or source item to inspect its source."); return
        selected_line = focus_line if type(focus_line) is int and focus_line > 0 else node.get("line")
        try: current = self.controller.source(file["id"]) if file.get("sha256") else None
        except ServiceError: current = None
        self.source_text.tag_configure("selected_source", background="#fff3a3", foreground="#1c1f23")
        if current and current["status"] == "current":
            notes = ["Whole file, read again from disk just now; its bytes match this scan."]
            if current.get("truncated"): notes.append(f"Only the first {len(current['lines']):,} of {current['line_count']:,} lines are shown.")
            if current.get("long_lines"): notes.append("Very long lines are shortened.")
            if file.get("parse_note"): notes.append(file["parse_note"])
            header = [f"{file['path']} · SHA-256 {file['sha256']}", "[" + " ".join(notes) + "]"]
            shown = (file["id"], file["sha256"], len(current["lines"]))
            if self.source_shown != shown:
                self.write(self.source_text, "\n".join(header) + "\n" + "\n".join(f"{number:>5}: {line}" for number, line in enumerate(current["lines"], 1)))
                self.source_shown = shown
            self.source_text.tag_remove("selected_source", "1.0", "end")
            if type(selected_line) is int and 0 < selected_line <= len(current["lines"]):
                row = len(header) + selected_line
                self.source_text.tag_add("selected_source", f"{row}.0", f"{row}.end")
                self.source_text.see(f"{row}.0")
        else:
            self.source_shown = None
            source_lines = file.get("source_preview", [])
            outside_preview = bool(selected_line and not any(item["line"] == selected_line for item in source_lines))
            if outside_preview:
                source_lines = [item for item in file.get("source_context", []) if abs(item["line"] - selected_line) <= 2]
            lines = [f"{line['line']:>4}: {line['text']}" + (" [line shortened]" if line.get("truncated") else "") for line in source_lines]
            prefix = f"{file['path']} · SHA-256 {file.get('sha256') or 'unavailable'}\n"
            if current and current["status"] == "changed":
                prefix += "[This file changed on disk after the scan. Showing what the scan captured; rescan to read the new version.]\n"
            elif file.get("parse_status"):
                prefix += "[" + str(file.get("parse_note") or "Listed but not parsed.") + " Its text could not be shown.]\n"
            else:
                prefix += "[The file could not be read again; showing what the scan captured.]\n"
            if outside_preview:
                has_selected_line = any(item["line"] == selected_line for item in source_lines)
                prefix += ("[Showing captured context around the selected item.]\n" if has_selected_line else
                           "[The selected line was not captured; any lines below are nearby context.]\n")
                if file.get("source_context_truncated"):
                    prefix += "[Some context lines were omitted or shortened by scan limits.]\n"
            elif file.get("source_preview_truncated"): prefix += "[Preview omits or shortens source lines.]\n"
            self.write(self.source_text, prefix + "\n".join(lines))
            position = next((i for i, line in enumerate(source_lines) if line["line"] == selected_line), None)
            if position is not None:
                row = prefix.count("\n") + 1 + position
                self.source_text.tag_add("selected_source", f"{row}.0", f"{row}.end")
                self.source_text.see(f"{row}.0")
        relevant = [self.controller.nodes[ident] for ident in self.controller.index.in_file(file["path"]) if self.controller.nodes[ident].get("line")]
        for item in sorted(relevant, key=lambda n: (n["line"], n["id"]))[:self.TREE_CHILD_LIMIT]:
            iid = self.source_list.insert("", "end", text=item.get("label") or item.get("name"), values=(item["line"], item["kind"]))
            self.source_ids[iid] = item["id"]

    def search_source(self):
        if self.busy or not self.controller.data:
            self.status.set("Open a repository before searching its source."); return
        query = self.source_query.get().strip()
        if len(query) < 2:
            self.match_summary.set("Type at least two characters."); return
        cancel = self.cancel_event = threading.Event()
        self.run_job(lambda: self.controller.search_source(query, cancel.is_set, self.report_progress), self.show_matches,
                     "Searching the scanned files…", cancellable=True)

    def show_matches(self, result):
        self.match_list.delete(*self.match_list.get_children()); self.match_ids = {}
        for match in result["matches"]:
            item = self.match_list.insert("", "end", text=match["path"], values=(match["line"], match["text"] + (" …" if match["truncated"] else "")))
            self.match_ids[item] = match
        stopped = {"result limit": " The list reached its limit; refine the text.", "time limit": " The search ran out of time; results are partial.",
                   "cancelled": " The search was stopped; results are partial."}.get(result["stopped"], "")
        skipped = result["files_changed_since_scan"] + result["files_unavailable"]
        self.match_summary.set(f"{len(result['matches'])} matching lines for “{result['query']}” in {result['files_searched']:,} of {result['files_total']:,} scanned files." + stopped +
                               (f" {skipped} files changed or could not be read and were not searched; rescan to include them." if skipped else ""))
        self.status.set("Search finished. " + result["limitation"])

    def open_match(self):
        if self.busy or not self.controller.data: return
        selected = self.match_list.selection()
        match = self.match_ids.get(selected[0]) if selected else None
        if not match or match["node_id"] not in self.controller.nodes:
            self.status.set("Choose a match first."); return
        self.select(match["node_id"])
        self.show_source(self.controller.nodes[match["node_id"]], focus_line=match["line"])
        self.tabs.select(self.source_tab)
        self.status.set("Showing the matching line; the surrounding item is selected in the map.")

    def open_source_item(self):
        if self.busy: return
        selected = self.source_list.selection()
        if selected and selected[0] in self.source_ids: self.select(self.source_ids[selected[0]])

    def excerpt_for(self, edge):
        if edge.get("snippet"):
            return edge["snippet"] + ("\n[Source excerpt shortened]" if edge.get("snippet_truncated") else "")
        file = self.controller.nodes.get("file:" + str(edge.get("path") or ""))
        line = next((x for x in (file or {}).get("source_preview", []) if x["line"] == edge.get("line")), None)
        return (line["text"] + (" [line shortened]" if line.get("truncated") else "")) if line else "Source excerpt not captured in the bounded preview."

    def prepare_connections(self, node, focus=None):
        self.connection_list.delete(*self.connection_list.get_children()); self.connection_ids = {}; self.write(self.connection_text, "Select a connection to inspect its evidence.")
        nearby = list(self.controller.index.incident(node["id"]))
        if focus is not None and any(edge is focus for edge in nearby):
            nearby = [focus] + [edge for edge in nearby if edge is not focus]
        for edge in nearby[:200]:
            outgoing = edge["source"] == node["id"]
            other = edge["target"] if outgoing else edge["source"]
            target = self.controller.nodes.get(other)
            if not target: continue
            item = self.connection_list.insert("", "end", text=target.get("label") or target.get("name"),
                values=("outgoing" if outgoing else "incoming", edge["kind"], edge["evidence"]))
            self.connection_ids[item] = (edge, other, outgoing)
        if len(nearby) > 200:
            self.write(self.connection_text, f"Showing 200 of {len(nearby)} connections. Select one to inspect its evidence.")

    def show_connection(self):
        if self.busy: return
        chosen = self.connection_list.selection()
        if not chosen or chosen[0] not in self.connection_ids: return
        edge, _, outgoing = self.connection_ids[chosen[0]]
        path, line = edge.get("path"), edge.get("line")
        file = self.controller.nodes.get("file:" + str(path or ""))
        digest = edge.get("sha256") or (file or {}).get("sha256")
        text = f"{'Outgoing' if outgoing else 'Incoming'} · {edge['kind']} ({edge['evidence']})\n"
        text += describe_connection(edge, outgoing, self.level_key()) + "\n"
        text += f"Source: {path}:{line} · SHA-256 {digest or 'unavailable'}\n" if path else "No individual source line was recorded for this link.\n"
        if path: text += "\n" + self.excerpt_for(edge) + "\n"
        if edge.get("basis"): text += "\n" + edge["basis"] + "\n"
        text += "\nInferred links are possible relationships, not observed execution."
        self.write(self.connection_text, text)

    def open_connection(self):
        if self.busy: return
        chosen = self.connection_list.selection()
        if chosen and chosen[0] in self.connection_ids: self.select(self.connection_ids[chosen[0]][1])

    def open_graph_connection(self, edge):
        if self.busy or not self.controller.data or not any(item is edge for item in self.controller.index.incident(edge.get("source"))): return
        if edge.get("source") not in self.controller.nodes or edge.get("target") not in self.controller.nodes: return
        self.select(edge["source"])
        self.prepare_connections(self.controller.nodes[edge["source"]], focus=edge)
        item = next((key for key, value in self.connection_ids.items() if value[0] is edge), None)
        if item is None: return
        self.connection_list.selection_set(item); self.connection_list.see(item); self.show_connection()
        self.tabs.select(self.connections_tab)
        self.status.set("Relationship evidence is shown in Links.")

    def open_connection_source(self):
        if self.busy or not self.controller.data: return
        chosen = self.connection_list.selection()
        if not chosen or chosen[0] not in self.connection_ids:
            self.status.set("Select a connection first."); return
        edge = self.connection_ids[chosen[0]][0]
        self.open_source_location(edge.get("path"), edge.get("line"))

    def open_source_location(self, path, line):
        file_id = "file:" + str(path or "")
        if file_id not in self.controller.nodes or type(line) is not int or line < 1:
            self.status.set("This step has no individual source location."); return
        candidates = [self.controller.nodes[ident] for ident in self.controller.index.at(path, line)]
        priority = {"callsite": 0, "dependency": 1, "function": 2}
        chosen_id = min(candidates, key=lambda n: (priority.get(n["kind"], 3), n["id"]))["id"] if candidates else file_id
        self.select(chosen_id)
        self.show_source(self.controller.nodes[chosen_id], focus_line=line)
        self.tabs.select(self.source_tab)
        self.status.set("Showing the captured source location; the file may have changed since scanning.")

    def prepare_practice(self, node):
        self.prepare_reading_practice()
        edges = self.controller.index.incident(node["id"])
        match = next(((e, "call") for e in edges if e["source"] == node["id"] and e["kind"] == "possible call" and e["evidence"] == "inferred"), None)
        if not match: match = next(((e, "callsite") for e in edges if e["target"] == node["id"] and e["kind"] == "contains written call" and e["evidence"] == "static"), None)
        if not match: match = next(((e, "definition") for e in edges if e["target"] == node["id"] and e["kind"] == "defines" and e["evidence"] == "static"), None)
        if not match: match = next(((e, "import") for e in edges if e["target"] == node["id"] and e["kind"] in ("imports", "includes") and e["evidence"] == "static"), None)
        self.practice_activity = match[1] if match else None
        self.write(self.practice_text, "")
        if not match:
            self.practice_prompt.set("No practice question is available for this item in the current scan.")
            self.practice_source.configure(state="disabled"); self.practice_ran.configure(state="disabled"); return
        edge, activity = match
        self.practice_prompt.set(f"At {edge.get('path') or 'the recorded source'}:{edge.get('line') or '?'}, which statement is supported?")
        labels = {"call": "A call may happen", "callsite": "A call is written", "definition": "A function is defined", "import": "An import or include is written"}
        self.practice_source.configure(text=labels[activity], state="normal"); self.practice_ran.configure(state="normal")

    def answer_practice(self, answer):
        if self.busy or not self.controller.data or not self.practice_activity: return
        repo, ident, sid = self.controller.context()
        try:
            result = self.controller.service.repository_practice(repo, ident, answer, sid, self.practice_activity)
            evidence = result["evidence"]
            self.write(self.practice_text, ("Supported by the scan. " if result["correct"] else "Try again. ") + result["explanation"] +
                f"\n\nSource: {evidence.get('path') or 'unavailable'}:{evidence.get('line') or '?'}\n" + self.excerpt_for(evidence))
        except ServiceError as exc: self.notify_error(exc)

    def reset_reading_practice(self):
        self.reading_questions = []; self.reading_index = 0; self.reading_choice.set(-1)
        self.reading_prompt.set("Select a function with captured source to find a reading question.")
        self.reading_source_note.set("")
        for variable in self.reading_choices: variable.set("")
        for option in self.reading_option_buttons: option.configure(state="disabled")
        for button in (self.reading_check, self.reading_next, self.reading_open): button.configure(state="disabled")
        self.write(self.reading_feedback, "")

    def prepare_reading_practice(self):
        self.reset_reading_practice()
        if self.busy or not self.controller.data: return
        repo, ident, sid = self.controller.context()
        try:
            self.reading_questions = self.controller.service.repository_reading_questions(repo, ident, self.level_key(), sid)
            if self.reading_questions:
                self.show_reading_question()
            elif self.controller.nodes[ident].get("kind") == "function":
                self.reading_prompt.set("No reading question is available for this function in the current scan. Inspect its source or choose another function.")
        except ServiceError as exc:
            self.reading_prompt.set(str(exc))

    def show_reading_question(self):
        if not self.reading_questions: return
        question = self.reading_questions[self.reading_index]
        self.reading_prompt.set(question["prompt"])
        evidence = question["evidence"]
        self.reading_source_note.set(f"Captured source: {evidence['path']}:{evidence['line']} · SHA-256 {evidence['sha256']}")
        for variable, choice in zip(self.reading_choices, question["choices"]): variable.set(choice)
        for option in self.reading_option_buttons: option.configure(state="normal")
        self.reading_choice.set(-1); self.write(self.reading_feedback, "")
        self.reading_check.configure(state="normal"); self.reading_open.configure(state="normal")
        self.reading_next.configure(state="normal" if len(self.reading_questions) > 1 else "disabled")

    def next_reading_question(self):
        if self.busy or not self.reading_questions: return
        self.reading_index = (self.reading_index + 1) % len(self.reading_questions)
        self.show_reading_question()

    def check_reading_answer(self):
        if self.busy or not self.reading_questions or not self.controller.data: return
        answer = self.reading_choice.get()
        if answer not in (0, 1): self.status.set("Choose an answer first."); return
        repo, ident, sid = self.controller.context(); question = self.reading_questions[self.reading_index]
        try:
            result = self.controller.service.repository_check_reading(repo, ident, self.level_key(), sid, question["kind"], answer)
            evidence = result["evidence"]
            text = ("Supported by the scan. " if result["correct"] else "Try the other choice. ") + result["explanation"]
            text += f"\n\n{evidence['path']}:{evidence['line']} · SHA-256 {evidence['sha256']}\n" + evidence["text"]
            if evidence.get("truncated"): text += " [line shortened]"
            self.write(self.reading_feedback, text + "\n\nThe source may have changed since this scan.")
        except ServiceError as exc: self.notify_error(exc)

    def open_reading_source(self):
        if self.busy or not self.reading_questions or not self.controller.data: return
        repo, ident, sid = self.controller.context(); question = self.reading_questions[self.reading_index]
        try:
            self.controller.service.repository_reading_questions(repo, ident, self.level_key(), sid)
            self.show_source(self.controller.nodes[ident], focus_line=question["evidence"]["line"])
            self.tabs.select(self.source_tab); self.status.set("Showing captured source; the question remains available in Practice.")
        except ServiceError as exc: self.notify_error(exc)

    def configure_model(self):
        if self.busy or self.audience_key() != "adult":
            self.status.set("Local model configuration is available in adult sessions only."); return
        if sys.platform != "linux":
            self.status.set("A network-isolated local model is not available on this platform yet."); return
        from tkinter import filedialog
        from .repository_assistant import LocalAssistant
        executable = filedialog.askopenfilename(parent=self.root, title="Choose a trusted local completion executable")
        if not executable: return
        model = filedialog.askopenfilename(parent=self.root, title="Choose a local GGUF model")
        if not model: return
        try:
            assistant = LocalAssistant(executable, model)
            if not assistant.available():
                self.status.set("The required Linux model sandbox is unavailable. No model was configured."); return
            self.controller.service.local_assistant = assistant
            self.clear_view()
            self.status.set("Local model configured for this session. Open the repository again to check isolation and enable the opt-in control.")
        except (OSError, ValueError) as exc: self.notify_error(exc)

    def explain(self):
        if self.busy or not self.controller.data: return
        topic, level = self.topic.get().lower(), self.level_key()
        if self.use_model.get():
            self.use_model.set(False)
            self.run_job(lambda: self.controller.explain(topic, level, True), lambda result: (self.write(self.learn_text, format_explanation(result)), self.status.set("Explanation ready.")), "Asking the isolated local model…")
        else:
            try: self.write(self.learn_text, format_explanation(self.controller.explain(topic, level)))
            except ServiceError as exc: self.notify_error(exc)

    def selected_call(self):
        selected = self.calls.curselection()
        if not selected or not self.controller.data: return None, None
        index = self.call_indices[selected[0]]
        calls = self.controller.nodes[self.controller.selected].get("call_sites", [])
        return index, calls[index]

    def refresh_callers(self):
        if self.busy or not self.controller.data: return
        self.prepare_callers(self.controller.nodes[self.controller.selected])

    def prepare_callers(self, node):
        self.caller_list.delete(*self.caller_list.get_children()); self.caller_ids = {}
        self.write(self.caller_text, "")
        if node.get("kind") != "function":
            self.caller_summary.set("Select a function to explore possible callers."); return
        repo, ident, sid = self.controller.context()
        try:
            result = self.controller.service.repository_possible_callers(repo, ident, self.level_key(),
                                                                           3 if self.callers_transitive.get() else 1, sid)
        except ServiceError as exc:
            self.caller_summary.set(str(exc)); return
        self.caller_summary.set(f"{len(result['rows'])} possible caller paths shown; {result['recorded_unresolved_calls']} recorded calls have unresolved targets. " +
                                ("The list reached its limit. " if result["truncated"] else "") +
                                ("Some call lists were truncated. " if result["call_sites_truncated"] else "") +
                                "These are not runtime observations.")
        for row in result["rows"]:
            caller = self.controller.nodes.get(row["caller_id"])
            if not caller: continue
            site = row["path"][0]
            item = self.caller_list.insert("", "end", text=caller.get("label") or caller.get("name") or row["caller_id"],
                                           values=(row["hops"], f"{site.get('path') or '?'}:{site.get('line') or '?'}"))
            self.caller_ids[item] = row
        self.write(self.caller_text, result["limitation"])

    def selected_caller(self):
        selection = self.caller_list.selection()
        return self.caller_ids.get(selection[0]) if selection else None

    def show_caller(self):
        if self.busy or not self.controller.data: return
        row = self.selected_caller()
        if not row: return
        lines = [row["prompt"]]
        for via in row["path"]:
            source, target = self.controller.nodes.get(via["source"], {}), self.controller.nodes.get(via["target"], {})
            lines.append(f"\n{source.get('label') or source.get('name') or via['source']} → {target.get('label') or target.get('name') or via['target']} (possible target: inferred)")
            lines.append(f"Written call (static): {via.get('path') or '?'}:{via.get('line') or '?'} · SHA-256 {via.get('sha256') or 'unavailable'}")
            if via.get("snippet"): lines.append(via["snippet"] + (" [excerpt shortened]" if via.get("snippet_truncated") else ""))
            if via.get("basis"): lines.append(via["basis"])
        lines.append("\nThis is a possible source-based path, not a runtime trace. Unresolved or unscanned calls may be missing.")
        self.write(self.caller_text, "\n".join(lines))

    def open_caller(self):
        if self.busy: return
        row = self.selected_caller()
        if row and row["caller_id"] in self.controller.nodes: self.select(row["caller_id"])
        else: self.status.set("Choose a possible caller first.")

    def open_caller_source(self):
        if self.busy: return
        row = self.selected_caller()
        if not row: self.status.set("Choose a possible caller first."); return
        via = row["path"][0]
        self.open_source_location(via.get("path"), via.get("line"))

    def route_from_caller(self):
        if self.busy or not self.controller.data: return
        row = self.selected_caller()
        if not row: self.status.set("Choose a possible caller first."); return
        target = self.controller.selected
        if row["caller_id"] == target:
            self.status.set("This may be a recursive call; open its source to inspect it."); return
        try:
            route = self.controller.path_between(row["caller_id"], target, self.level_key(), True, False)
            if route["status"] == "ready":
                expected = [(via["source"], via["target"], via.get("path"), via.get("line")) for via in row["path"]]
                actual = [(via.get("source"), via.get("target"), via.get("path"), via.get("line"))
                          if via and via.get("direction") == "forward" and via.get("kind") == "possible call" else None
                          for via in (step.get("via") for step in route["steps"][1:])]
                if actual != expected:
                    notice = "The map found a different path from the selected possible caller. Re-select the destination to inspect the original call sites in Possible callers."
                    route["message"] = notice + " " + route["message"]
                    route["limitation"] += " " + notice
                    self.status.set(notice)
            self.display_route(route); self.tabs.select(self.guided_tab)
        except ServiceError as exc: self.notify_error(exc)

    def show_call_details(self):
        if self.busy or not self.controller.data: return
        _, call = self.selected_call()
        if not call: return
        if call.get("target_id"):
            text = "The target is a source-based possibility, not an observed call. " + (call.get("basis") or "")
        else:
            from .repository_learning import explain_unresolved_call
            text = explain_unresolved_call(call, self.level_key())
        self.call_detail.set(text)

    def compare_call(self):
        if self.busy: return
        index, call = self.selected_call()
        if call is None: self.status.set("Select a written call first."); return
        try:
            self.write(self.learn_text, format_comparison(self.controller.compare(index, self.level_key())))
            self.tabs.select(self.learn_tab); self.status.set("Call comparison is shown in Learn.")
        except ServiceError as exc: self.notify_error(exc)

    def follow_call(self, key):
        if self.busy: return
        _, call = self.selected_call()
        if call and call.get(key) in self.controller.nodes: self.select(call[key])
        else: self.status.set("That call does not have a mapped item or resolved target in this scan.")

    def save_label(self):
        if self.busy or not self.controller.data: return
        try:
            result = self.controller.label(self.label.get()); self.update_tree_row(self.controller.selected); self.refresh_destinations(); self.draw_graph(); self.show_selected()
            self.steps.delete(0, "end"); self.write(self.step_text, ""); self.status.set("Label saved locally." if result["label"] else "Label removed from this item.")
        except ServiceError as exc: self.notify_error(exc)

    def remove_label(self):
        if self.busy or not self.controller.data: return
        self.label.set(""); self.save_label()

    def open_inventory_label(self):
        if self.busy or not self.controller.data: return
        selected = self.label_inventory.selection()
        if not selected or selected[0] not in self.label_inventory_ids:
            self.status.set("Choose a saved label first."); return
        ident = self.label_inventory_ids[selected[0]]
        if ident is None:
            self.label_tools.select(self.repair_labels)
            self.status.set("This label is unmatched. Select a current item and attach the label below."); return
        self.select(ident)

    def toggle_review(self):
        if self.busy or not self.controller.data: return
        repo, ident, sid = self.controller.context(); node = self.controller.nodes[ident]
        reviewed = not (node.get("reviewed") and not node.get("review_needs_refresh"))
        try:
            self.controller.service.repository_progress(repo, ident, reviewed, sid)
            node["reviewed"], node["review_needs_refresh"] = reviewed, False
            self.review_note.configure(text="You marked this item reviewed locally." if reviewed else "Not marked reviewed. A mark is optional and is not a score.")
            self.status.set("Marked reviewed locally; this is not a test score." if reviewed else "Review mark removed.")
        except ServiceError as exc: self.notify_error(exc)

    def rebind_label(self):
        if self.busy or not self.controller.data: return
        index = self.unmatched_box.current()
        if index < 0 or index >= len(self.unmatched): self.status.set("Choose an unmatched label first."); return
        old = self.unmatched[index]; repo, ident, sid = self.controller.context()
        try:
            result = self.controller.service.repository_rebind_label(repo, old["node_id"], ident, sid)
            self.controller.nodes[ident]["label"] = result["label"]
            self.controller.data["unmatched_labels"] = [x for x in self.controller.data["unmatched_labels"] if x["node_id"] != old["node_id"]]
            self.controller.route = None; self.update_tree_row(ident); self.refresh_destinations(); self.draw_graph(); self.show_selected()
            self.steps.delete(0, "end"); self.write(self.step_text, ""); self.status.set("Saved label attached to this item.")
        except ServiceError as exc: self.notify_error(exc)

    def reconnect(self):
        if self.busy or not self.controller.data or self.controller.audience != "adult": return
        index = self.project_box.current()
        if index < 0 or index >= len(self.projects): self.status.set("Choose a saved collection first."); return
        project, data, path = self.projects[index], self.controller.data, self.opened_path
        audience, isolation = self.controller.audience, self.isolation_key()
        def work():
            self.controller.service.repository_reconnect(data["location_id"], project["id"], data["scan_id"])
            return self.controller.open(path, audience, isolation)
        self.run_job(work, lambda result: self.loaded(result, path), "Connecting saved labels and rescanning…")

    def refresh_shelf(self):
        if self.busy or self.audience_key() != "adult": return
        try:
            self.shelf_items = self.controller.shelf("adult")
            self.shelf_box.configure(state="readonly", values=[f"{x['name']} · {x['path']}" for x in self.shelf_items]); self.shelf_box.set("")
            self.status.set("Remembered folder paths are stored in this local workspace.")
        except ServiceError as exc: self.notify_error(exc)

    def use_shelf(self):
        if self.busy or self.audience_key() != "adult": return
        index = self.shelf_box.current()
        if 0 <= index < len(self.shelf_items):
            self.path.set(self.shelf_items[index]["path"]); self.status.set("Folder selected. Open it to scan again.")
        else: self.status.set("Choose a remembered folder first.")

    def remember(self):
        if self.busy or not self.controller.data or self.audience_key() != "adult": return
        try: self.controller.remember(); self.refresh_shelf(); self.status.set("Folder remembered in this local workspace.")
        except ServiceError as exc: self.notify_error(exc)

    def forget(self):
        if self.busy or self.audience_key() != "adult": return
        index = self.shelf_box.current()
        if not 0 <= index < len(self.shelf_items): self.status.set("Choose a remembered folder first."); return
        try:
            self.controller.service.repository_forget(self.shelf_items[index]["location_id"]); self.refresh_shelf()
            self.status.set("Folder forgotten; labels and review marks were kept.")
        except ServiceError as exc: self.notify_error(exc)

    def refresh_destinations(self):
        self.labeled_nodes = [n for n in self.controller.nodes.values() if n.get("label")]
        self.dest_ids = [n["id"] for n in self.labeled_nodes]
        self.destination.configure(values=[f"{self.controller.nodes[i]['label']} · {self.controller.nodes[i].get('path') or 'repository'}" + (" · needs review" if self.controller.nodes[i].get("label_needs_review") else "") for i in self.dest_ids]); self.destination.set("")
        self.label_inventory.delete(*self.label_inventory.get_children()); self.label_inventory_ids = {}
        for node in sorted((n for n in self.controller.nodes.values() if n.get("label")), key=lambda n: (n["label"].casefold(), n.get("path") or "", n["id"])):
            item = self.label_inventory.insert("", "end", text=node["label"], values=("Needs review" if node.get("label_needs_review") else "Current", node.get("path") or "repository"))
            self.label_inventory_ids[item] = node["id"]
        for label in sorted((self.controller.data or {}).get("unmatched_labels", []), key=lambda n: (n["label"].casefold(), n["node_id"])):
            item = self.label_inventory.insert("", "end", text=label["label"], values=("Unmatched", label.get("path") or "unknown"))
            self.label_inventory_ids[item] = None

    def build_guide(self):
        if self.busy or not self.controller.data: return
        goal = {"Understand a function": "understand", "Explore written calls": "calls", "Explore a dependency": "dependencies"}[self.goal.get()]
        try: self.display_route(self.controller.guide(goal, self.level_key(), self.prefer_unreviewed.get()))
        except ServiceError as exc: self.notify_error(exc)

    def build_label_path(self):
        if self.busy or not self.controller.data: return
        index = self.destination.current()
        if index < 0 or index >= len(self.dest_ids): self.status.set("Choose a labeled destination first."); return
        try: self.display_route(self.controller.path_to_label(self.dest_ids[index], self.level_key(), self.include_inferred.get(), self.include_organization.get()))
        except ServiceError as exc: self.notify_error(exc)

    def set_route_start(self):
        if self.busy or not self.controller.data: return
        ident = self.controller.selected
        node = self.controller.nodes[ident]
        self.route_start = ident
        self.route_start_note.set("Start: " + str(node.get("label") or node.get("name") or ident))
        self.status.set("Path start set. Select a destination in the map or item list.")

    def build_item_path(self):
        if self.busy or not self.controller.data: return
        if self.route_start not in self.controller.nodes:
            self.status.set("Select an item and set it as the path start first."); return
        try:
            self.display_route(self.controller.path_between(self.route_start, self.controller.selected, self.level_key(),
                                                           self.include_inferred.get(), self.include_organization.get()))
        except ServiceError as exc: self.notify_error(exc)

    def display_route(self, route):
        self.steps.delete(0, "end"); self.write(self.step_text, route["message"] + "\n" + route["limitation"])
        for step in route["steps"]:
            node = self.controller.nodes[step["node_id"]]
            self.steps.insert("end", f"{node.get('label') or node.get('name')} · {step['topic']}")
        self.draw_graph()
        if route["steps"]:
            self.steps.selection_set(0); self.show_step()

    def show_step(self):
        if self.busy or not self.controller.route: return
        selection = self.steps.curselection()
        if not selection: return
        step = self.controller.route["steps"][selection[0]]
        self.select(step["node_id"])
        via = step.get("via")
        connection = ""
        if via:
            connection = f"\n{via.get('direction')} · {via['kind']} ({via['evidence']}) · {via.get('path') or 'location unavailable'}:{via.get('line') or '?'}\n"
            if via.get("snippet"): connection += via["snippet"] + "\n"
            if via.get("basis"): connection += via["basis"] + "\n"
        self.write(self.step_text, step["prompt"] + connection + "\n" + format_explanation(step["explanation"]) + "\n" + self.controller.route["limitation"])

    def move_step(self, offset):
        if self.busy or not self.controller.route or not self.controller.route["steps"]: return
        current = self.steps.curselection()
        index = min(len(self.controller.route["steps"]) - 1, max(0, (current[0] if current else 0) + offset))
        self.steps.selection_clear(0, "end"); self.steps.selection_set(index); self.steps.see(index); self.show_step()

    def open_step_source(self):
        if self.busy or not self.controller.route: return
        selected = self.steps.curselection()
        if not selected: self.status.set("Select a path step first."); return
        step = self.controller.route["steps"][selected[0]]
        via = step.get("via") or {}
        explanation = step["explanation"]
        source = explanation.get("source") or {}
        citation = next(iter(explanation.get("citations") or []), {})
        if via.get("path") and via.get("line"):
            self.open_source_location(via["path"], via["line"])
        else:
            self.open_source_location(source.get("path"), citation.get("line") or source.get("line"))


def main(argv=None):
    parser = argparse.ArgumentParser(prog="elucidary", description="Elucidary native desktop, no local web server")
    parser.add_argument("--workspace", type=Path, default=default_workspace(), help="local workspace for labels and remembered folders")
    parser.add_argument("--local-assistant-executable", help="trusted local llama.cpp completion executable")
    parser.add_argument("--local-assistant-model", help="local GGUF model file")
    parser.add_argument("--self-check", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if bool(args.local_assistant_executable) != bool(args.local_assistant_model):
        parser.error("supply both local assistant executable and model")
    root, service = None, None
    try:
        assistant = None
        if args.local_assistant_executable:
            from .repository_assistant import LocalAssistant
            assistant = LocalAssistant(args.local_assistant_executable, args.local_assistant_model)
        import tkinter as tk
        root = tk.Tk()
        if args.self_check:
            root.withdraw()
        service = Service(args.workspace, local_assistant=assistant)
        DesktopApp(root, DesktopController(service))
        if args.self_check:
            root.update_idletasks(); service.close(); root.destroy(); return 0
        root.mainloop()
        return 0
    except Exception as exc:
        if service:
            service.close()
        if root:
            root.destroy()
        message = f"Elucidary could not start: {exc}"
        if sys.stderr is not None:
            print(message, file=sys.stderr)
        else:
            try:
                from tkinter import messagebox
                messagebox.showerror("Elucidary", message)
            except Exception:
                pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

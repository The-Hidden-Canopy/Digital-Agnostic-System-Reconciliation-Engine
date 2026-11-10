"""Source-grounded, deterministic explanations for repository snapshots."""
from __future__ import annotations

import hashlib
import re


def evidence_lines(lines, numbers):
    points = sorted(set(n for n in numbers if isinstance(n, int) and 1 <= n <= len(lines)))
    return [{"line": n, "text": lines[n - 1][:500], "truncated": len(lines[n - 1]) > 500} for n in points[:20]]


def _incident(edges, node_id):
    """Connections touching one item, from a scan index when given, else by reading the edge list."""
    lookup = getattr(edges, "incident", None)
    if lookup is not None:
        return lookup(node_id)
    return [e for e in edges if e["source"] == node_id or e["target"] == node_id]


def _nodes_by_id(nodes):
    return nodes if isinstance(nodes, dict) else {node["id"]: node for node in nodes}


def explain_unresolved_call(call, level):
    """Explain a lack of static resolution without claiming the target is absent."""
    if level == "beginner":
        return "The scan could not tell what this call uses. That does not mean it has no target."
    form = call.get("unresolved_form")
    if form == "local":
        detail = "The called name is a parameter, local variable or local function value in the enclosing function, so the scan does not match it to a definition elsewhere."
    elif form == "arity":
        detail = "A definition with this name was found, but the number of written arguments does not fit its parameters, so it is not offered as a target."
    elif form == "complex":
        detail = "The written callee is not a simple name or a recognized member form."
    elif form == "member":
        detail = "The scan did not establish a unique target for this member or attribute call."
    else:
        detail = "The scan did not establish a unique supported target for this written name."
    if level == "explore":
        return detail + " Inspect the call and consider where its name or value could come from. The target may still exist."
    return detail + " Dynamic dispatch, rebinding, unsupported syntax and unscanned code remain possible; no execution was observed."


def reading_challenges(node, level):
    """Build small open-book questions only from captured function evidence."""
    if level not in ("beginner", "explore", "technical"):
        raise ValueError("invalid learning level")
    shape = node.get("shape")
    if node.get("kind") != "function" or not isinstance(shape, dict) or not node.get("path") or not node.get("sha256"):
        return []
    lines = {item["line"]: item for item in shape.get("evidence_lines", [])
             if type(item.get("line")) is int and isinstance(item.get("text"), str)}
    questions = []

    def add(kind, prompt, correct, other, evidence, explanation):
        choices = [correct, other]
        reverse = hashlib.sha256((node["id"] + ":" + kind).encode("utf-8")).digest()[0] % 2
        if reverse: choices.reverse()
        questions.append({"kind": kind, "prompt": prompt, "choices": choices, "correct_index": int(bool(reverse)),
                          "evidence": {"path": node["path"], "line": evidence["line"], "text": evidence["text"],
                                       "truncated": bool(evidence.get("truncated")), "sha256": node["sha256"]},
                          "explanation": explanation})

    name = str(node.get("name") or "").rsplit(".", 1)[-1].rsplit("::", 1)[-1]
    header = (node.get("snippet") or "").split("\n", 1)[0]
    parameters = shape.get("parameters", [])
    parameter_names = {item.get("name") for item in parameters if isinstance(item.get("name"), str)}
    for parameter in parameters:
        param_name, line = parameter.get("name"), lines.get(parameter.get("line"))
        if (not isinstance(param_name, str) or not param_name.isidentifier() or len(param_name) > 80 or
                not name.isidentifier() or len(name) > 80 or name in parameter_names or not line or
                param_name not in line["text"] or name not in header):
            continue
        prompt = {"beginner": "Which name did the scan record as a parameter? A parameter can receive a value.",
                  "explore": "Which name is a recorded parameter rather than the function's name?",
                  "technical": "Which identifier is recorded as a formal parameter rather than the declaration name?"}[level]
        add("parameter", prompt, param_name, name, line,
            "The scan recorded this parameter in the source. Its runtime value is not known from the definition.")
        break

    start = node.get("line")
    returns = {line for line in shape.get("return_lines", []) if type(line) is int}
    if type(start) is int and start not in returns and header and not re.search(r"\breturn\b", header[:140]):
        for number in sorted(returns):
            line = lines.get(number)
            if not line or not re.search(r"\breturn\b", line["text"][:140]):
                continue
            snippet = lambda item: item[:140] + (" …" if len(item) > 140 else "")
            prompt = {"beginner": "Which line did the scan record as an explicit return? A return can send a value back.",
                      "explore": "Which line did the scan record as an explicit return in this function?",
                      "technical": "Which recorded line contains explicit return syntax in this function body?"}[level]
            add("return", prompt, f"Line {number}: {snippet(line['text'])}", f"Line {start}: {snippet(header)}", line,
                "This is a recorded return statement. The scan does not establish whether it ran or what value it returned.")
            break
    constructs = {"conditional": ("choice", r"\bif\b"), "loop": ("loop", r"\b(?:for|foreach|while|do|loop)\b"),
                  "selection": ("selection", r"\bswitch\b"), "pattern match": ("pattern match", r"\bmatch\b"),
                  "exception handling": ("try block", r"\btry\b"), "raise": ("raised error", r"\b(?:raise|throw)\b"),
                  "await": ("await expression", r"\b(?:await|co_await)\b")}
    for feature in shape.get("features", []):
        kind, number = feature.get("kind"), feature.get("line")
        if kind not in constructs or type(number) is not int or number == start or type(start) is not int or not header:
            continue
        label, token = constructs[kind]; line = lines.get(number)
        if not line or not re.search(token, line["text"][:140]) or re.search(token, header[:140]):
            continue
        snippet = lambda item: item[:140] + (" …" if len(item) > 140 else "")
        prompt = {"beginner": f"Which line did the scan record as a {label}?",
                  "explore": f"Find the recorded {label} in the function. Which source line is it?",
                  "technical": f"Which line did the scanner record as {kind} syntax in this function body?"}[level]
        add("structure", prompt, f"Line {number}: {snippet(line['text'])}", f"Line {start}: {snippet(header)}", line,
            "The scan recorded this construct in source. Its presence does not show which path ran or how often.")
        break
    return questions


def explain(node, edges, topic, level):
    source = {"path": node.get("path"), "line": node.get("line"), "sha256": node.get("sha256")}
    facts, links, citations, call_sites = [], [], [], []
    shape = node.get("shape")
    if topic == "overview":
        if node.get("parse_note"):
            facts.append(node["parse_note"])
        if node["kind"] == "function":
            facts.append("This item is a function definition found in the scanned source.")
            if level == "beginner":
                facts.append("A function groups instructions under a name so other code can refer to them.")
            elif level == "explore":
                facts.append("Find the definition, then follow a possible connection and compare the two source locations.")
            elif node.get("language"):
                facts.append("The scanner classified this source as " + node["language"] + ".")
            if shape and shape.get("async") is True:
                facts.append("The definition is marked async in the source.")
            if shape:
                facts.append("Its input names and explicit return statements can be inspected using the other topics.")
        elif node["kind"] == "file":
            facts.append("This file contains the scanned source items connected to it in the map.")
        elif node["kind"] == "callsite":
            facts.append("This is a call expression written in the source; it does not establish that the call ran.")
        elif node["kind"] == "dependency":
            facts.append("This is an import or include written in the source; installation and execution were not checked.")
        else:
            facts.append("This is the folder selected for the scan.")
    elif topic == "inputs":
        if not shape:
            facts.append("Input details were not extracted for this item.")
        else:
            params = shape.get("parameters", [])
            if level == "beginner":
                facts.append("Parameters are names or patterns written for values a function may receive.")
            elif level == "explore":
                facts.append("Compare these inputs with a possible call; the scan cannot tell which values actually arrived.")
            if not params:
                facts.append("No parameters were recorded in this definition.")
            else:
                prefix = "The source names these inputs" if level != "technical" else "Recorded formal parameter syntax"
                details = [p["name"] + (" (" + p["kind"] + ")" if level == "technical" else "") for p in params]
                facts.append(prefix + ": " + ", ".join(details) + ".")
                if any(p["kind"] == "pattern" for p in params):
                    facts.append("A pattern or unnamed parameter is shown as a placeholder; inspect the source for its details.")
            if shape.get("parameters_truncated"):
                facts.append("The parameter list was truncated; inspect the source for the rest.")
            facts.append("The definition alone does not tell us which values were passed at runtime.")
    elif topic == "returns":
        if not shape:
            facts.append("Return-statement details were not extracted for this item.")
        else:
            lines = shape.get("return_lines", [])
            if level == "beginner":
                facts.append("A return statement can send a value back to the code that used the function.")
            elif level == "technical":
                facts.append("Only explicit return syntax in this function body was counted; nested function bodies were excluded.")
            if lines:
                facts.append("Explicit return statements appear on " + ("line " if len(lines) == 1 else "lines ") + ", ".join(map(str, lines)) + ".")
            else:
                facts.append("No explicit return statement was recorded in this function body.")
            if shape.get("returns_truncated"):
                facts.append("The list of return locations was truncated.")
            if shape.get("return_detail") == "explicit_only_arrow_expressions_excluded":
                facts.append("An expression-bodied arrow function may return without a return statement.")
            elif shape.get("return_detail") == "explicit_only_tail_expressions_excluded":
                facts.append("A Rust function can return its final expression without a return statement; those are not listed.")
            elif shape.get("return_detail") == "explicit_only_expression_bodies_excluded":
                facts.append("An expression-bodied member (=>) returns without a return statement; those are not listed.")
            facts.append("These locations do not show which branch ran or what value was returned.")
    elif topic == "calls":
        recorded = node.get("call_sites")
        if recorded is None:
            facts.append("Written call-site details were not extracted for this item.")
        else:
            if level == "beginner":
                facts.append("A written call is a place where code asks to use something; its target may not be known from the scan.")
            elif level == "explore":
                facts.append("Compare each written call with its possible target, if one is available.")
            else:
                facts.append("These are syntactic call sites in the recorded body; resolution is conservative and may be incomplete.")
            facts.append(f"The scan recorded {len(recorded)} call site{'' if len(recorded) == 1 else 's'} for this item; up to 20 are shown.")
            call_sites = recorded[:20]
            if node.get("call_sites_truncated") or len(recorded) > 20:
                facts.append("The call-site list was truncated; inspect the source for the rest.")
            facts.append("The scan covers supported body syntax and may miss calls, including decorators or default expressions.")
            facts.append("Written calls and inferred targets do not establish that a call ran or which values were passed.")
    elif topic == "structure":
        if not shape or "features" not in shape:
            facts.append("Control-flow details were not extracted for this item.")
        else:
            if level == "beginner":
                facts.append("Choices and loops are places where code may take different paths or repeat work.")
            elif level == "explore":
                facts.append("Find each listed construct in the source and discuss what conditions surround it.")
            else:
                facts.append("These are recorded syntax constructs, not measured branch coverage or an exhaustive control-flow graph.")
            features = shape.get("features", [])
            if features:
                facts.append("Recorded constructs: " + "; ".join(f'{f["kind"]} on line {f["line"]}' for f in features) + ".")
            else:
                facts.append("No supported choice, loop or exception construct was recorded in this body.")
            if shape.get("features_truncated"):
                facts.append("The construct list was truncated; inspect the source for the rest.")
            facts.append("A construct appearing in source does not prove it ran.")
    else:
        nearby = _incident(edges, node["id"])
        facts.append(f"The scan recorded {len(nearby)} connections for this item; the list below shows up to 20.")
        if level == "beginner":
            facts.append("A connection is a link found in source or a possible link inferred from it.")
        elif level == "technical":
            facts.append("Edges retain their evidence classification and source location when available.")
        for edge in nearby[:20]:
            links.append({"node_id": edge["target"] if edge["source"] == node["id"] else edge["source"],
                          "direction": "outgoing" if edge["source"] == node["id"] else "incoming",
                          "kind": edge["kind"], "evidence": edge["evidence"],
                          "path": edge.get("path"), "line": edge.get("line"), "basis": edge.get("basis"),
                          "snippet": edge.get("snippet"), "snippet_truncated": edge.get("snippet_truncated", False), "sha256": edge.get("sha256")})
        facts.append("Inferred connections are possibilities, not runtime observations.")
    if shape and topic in ("inputs", "returns", "structure"):
        wanted = ({p["line"] for p in shape.get("parameters", [])} if topic == "inputs" else
                  set(shape.get("return_lines", [])) if topic == "returns" else {f["line"] for f in shape.get("features", [])})
        citations = [line for line in shape.get("evidence_lines", []) if line["line"] in wanted]
        if len(wanted) > len(citations):
            facts.append("Some source lines are outside this bounded excerpt; inspect the file for the rest.")
    return {"topic": topic, "level": level, "facts": facts, "source": source, "citations": citations, "connections": links, "call_sites": call_sites,
            "limitation": "This describes scanned syntax, not purpose, correctness, or observed execution. Source may have changed since the scan."}


def _local_evidence_cards(node, result):
    """Produce bounded reading prompts from scan evidence, never interpret source as instructions."""
    level, topic = result["level"], result["topic"]
    prompts = {
        "beginner": {"overview": "What name or symbol can you spot here?", "inputs": "Which input name can you find in this line?", "returns": "What is written after return?", "structure": "Can you find the choice or repetition in this line?", "calls": "What is the code asking to use here?", "connections": "What words in this line suggest a connection?"},
        "explore": {"overview": "Describe what this line declares, then compare it with the surrounding source.", "inputs": "Compare this input with an argument at a written call, if one is available.", "returns": "What expression is written here, and what would you need to know to determine its value?", "structure": "What condition or repetition is written here? Which details are missing?", "calls": "Compare the written call with its possible target; what is still unknown?", "connections": "Compare this source with the linked item and check the evidence label."},
        "technical": {"overview": "Inspect the declaration and its surrounding scope.", "inputs": "Inspect the parameter syntax and any surrounding binding context.", "returns": "Inspect this explicit return and distinguish syntax from reachable runtime behavior.", "structure": "Inspect this recorded construct and the conditions needed to reach it.", "calls": "Inspect the call syntax, resolution basis and possible dynamic dispatch.", "connections": "Inspect the evidence classification, direction and resolution basis."},
    }
    candidates = []
    source = result["source"]
    if topic == "overview" and node.get("snippet") and source.get("path") and source.get("line"):
        first_line = node["snippet"].split("\n", 1)[0]
        candidates.append({"path": source["path"], "line": source["line"], "sha256": source.get("sha256"),
                           "text": first_line[:500], "truncated": len(first_line) > 500, "evidence": "static"})
    for line in result["citations"]:
        candidates.append({"path": source.get("path"), "line": line["line"], "sha256": source.get("sha256"),
                           "text": line["text"], "truncated": line.get("truncated", False), "evidence": "static"})
    if topic == "calls":
        candidates.extend({"path": site.get("path"), "line": site.get("line"), "sha256": site.get("sha256"),
                           "text": site.get("snippet"), "truncated": site.get("snippet_truncated", False),
                           "evidence": "static", "resolution": site.get("resolution", "unresolved")} for site in result["call_sites"])
    if topic == "connections":
        candidates.extend({"path": link.get("path"), "line": link.get("line"), "sha256": link.get("sha256"),
                           "text": link.get("snippet"), "truncated": link.get("snippet_truncated", False),
                           "evidence": link.get("evidence"), "basis": link.get("basis")} for link in result["connections"])
    cards = []
    for candidate in candidates:
        if not candidate.get("path") or not isinstance(candidate.get("line"), int) or not candidate.get("text"):
            continue
        cards.append({"prompt": prompts[level][topic], "citation": candidate})
        if len(cards) == 4:
            break
    return cards


def explain_with_provider(node, edges, topic, level):
    """Local provider boundary. Repository data is never sent to an external service."""
    result = explain(node, edges, topic, level)
    try:
        result["evidence_cards"] = _local_evidence_cards(node, result)
        result["provider"] = {"name": "Local evidence guide", "mode": "local", "fallback": False}
    except Exception:
        # Keep the original bounded, deterministic explanation available if enrichment fails.
        result["evidence_cards"] = []
        result["provider"] = {"name": "Local basic explanation", "mode": "local", "fallback": True}
    return result


def compare_call_with_target(call, target, level):
    """Compare written syntax with an inferred candidate, without inventing bindings."""
    shape = target.get("shape")
    raw_lines = (target.get("snippet") or "").splitlines()
    definition = [{"line": target["line"] + i, "text": text[:500], "truncated": len(text) > 500}
                  for i, text in enumerate(raw_lines[:3])] if target.get("line") else []
    prompts = {
        "beginner": "Find the expressions in the call and the input names in the possible definition. What can you see in each place?",
        "explore": "Compare the call syntax with the possible definition. What extra information would you need to know which values arrive?",
        "technical": "Inspect the call syntax, candidate resolution and formal parameters; consider dynamic dispatch, defaults, keyword arguments and expansion.",
    }
    return {
        "prompt": prompts[level],
        "call": {key: call.get(key) for key in ("written", "written_truncated", "written_arguments", "path", "line", "snippet", "snippet_truncated", "sha256")},
        "target": {"node_id": target["id"], "path": target.get("path"), "line": target.get("line"), "sha256": target.get("sha256"),
                   "definition": definition, "definition_truncated": len(raw_lines) > len(definition) or any(line["truncated"] for line in definition),
                   "parameters": shape.get("parameters", []) if shape else None,
                   "parameters_truncated": bool(shape and shape.get("parameters_truncated"))},
        "relationship": {"evidence": "inferred", "basis": call.get("basis")},
        "limitation": "The target is a possible static match, not an observed call. Counts and parameter names are written syntax, not a mapping of arguments to parameters; defaults, keywords, expansion and runtime dispatch may change the result.",
    }


MAX_PATH_SEARCH_ITEMS = 60_000


def path_between_items(nodes, edges, start_id, target_id, level, include_inferred, include_organization, *, labeled=False):
    """Find a bounded undirected exploration path while preserving each edge's direction."""
    by_id = _nodes_by_id(nodes)
    if start_id not in by_id or target_id not in by_id:
        raise ValueError("path endpoints must belong to the current scan")

    def adjacent(ident):
        """This item's usable links, in the same order whether read from an index or an edge list."""
        found = []
        for edge in _incident(edges, ident):
            a, b = edge.get("source"), edge.get("target")
            if a not in by_id or b not in by_id or edge.get("evidence") not in ("static", "inferred"):
                continue
            if edge["evidence"] == "inferred" and not include_inferred:
                continue
            if not include_organization and edge.get("kind") in ("contains", "defines"):
                continue
            if a == ident:
                found.append((b, edge, "forward"))
            if b == ident:
                found.append((a, edge, "reverse"))
        found.sort(key=lambda x: (x[1]["evidence"] != "static", x[0], x[1].get("kind", ""), x[1].get("line") or 0, x[2]))
        return found
    queue, parent, explored = [(start_id, 0)], {start_id: None}, 0
    for current, distance in queue:
        if current == target_id:
            break
        if distance >= 8:
            continue
        explored += 1
        if explored > MAX_PATH_SEARCH_ITEMS:
            break
        for other, edge, direction in adjacent(current):
            if other not in parent:
                parent[other] = (current, edge, direction)
                queue.append((other, distance + 1))
    limit = "This is an exploration path through the scanned graph, not a runtime trace. Reverse steps do not reverse the source relationship."
    if target_id not in parent:
        return {"status": "unavailable", "goal": "label" if labeled else "item", "steps": [],
                "message": ("The search stopped after visiting many items without reaching the destination; a path may still exist. "
                            if explored > MAX_PATH_SEARCH_ITEMS else "") +
                           "No path was found within eight links using these choices. Try including other connection types or choose another item.",
                "limitation": limit + " Unscanned or unresolved relationships are not included."}
    segments, current = [], target_id
    while parent[current] is not None:
        previous, edge, direction = parent[current]
        segments.append((current, edge, direction))
        current = previous
    segments.reverse()
    prompts = {
        "possible call": ("This may be a call between the two parts. Find the call in the source; does it prove the call ran?",
                          "Compare the written call with this possible target. What could make the target different?",
                          "Inspect the call site and resolution basis; consider rebinding and dynamic dispatch."),
        "possible target": ("This is a possible target for a written call. What can the source tell you for sure?",
                            "Compare the call text with the possible target; what is still uncertain?",
                            "Inspect the syntactic call, target candidate and conservative resolution basis."),
        "possible local module": ("This might point to a local file. What does the import or include actually say?",
                                  "Compare the written dependency with this possible local file. What else might resolve it?",
                                  "Inspect the dependency syntax and inferred local-module resolution basis."),
        "contains written call": ("Find the written call. Does a written call prove that it ran?",
                                 "Find the call in its surrounding source and compare its possible target, if any.",
                                 "Inspect the call expression, its enclosing source and unresolved dispatch."),
        "defines": ("Find where the source defines this part. Which file contains it?",
                    "Compare the definition with its file and its surrounding source.",
                    "Inspect the definition location and its enclosing source scope."),
        "contains": ("Find the file in the project. What is its name?",
                     "Locate this file in the project and consider what other files you might explore.",
                     "Inspect the scanned repository-to-file containment relationship."),
        "imports": ("Find the import written in this file. What name does it mention?",
                    "Compare the import with the dependency name; does this tell you it ran?",
                    "Inspect the import syntax; installation and runtime loading were not checked."),
        "includes": ("Find the include written in this file. What name does it mention?",
                     "Compare the include with the dependency name; does this tell you it ran?",
                     "Inspect the include syntax; build configuration and runtime behavior were not checked."),
    }
    steps = [{"node_id": start_id, "topic": "overview", "prompt": "Start at the selected item.", "via": None,
              "explanation": explain_with_provider(by_id[start_id], edges, "overview", level)}]
    for ident, edge, direction in segments:
        via = {k: edge.get(k) for k in ("source", "target", "kind", "evidence", "path", "line", "sha256", "snippet", "snippet_truncated", "basis")}
        via["direction"] = direction
        prompt = prompts.get(edge["kind"], ("What does this source connection show?", "Compare the linked items and what is uncertain.", "Inspect edge direction, evidence and basis."))[("beginner", "explore", "technical").index(level)]
        if direction == "reverse":
            prompt += " You followed this link backward; its source direction is unchanged."
        steps.append({"node_id": ident, "topic": "connections", "prompt": prompt, "via": via,
                      "explanation": explain_with_provider(by_id[ident], edges, "connections", level)})
    destination = "saved label" if labeled else "selected destination"
    return {"status": "ready", "goal": "label" if labeled else "item", "steps": steps,
            "message": f"You are already at this {destination}." if not segments else f"Follow the highlighted path to your {destination}. Each link describes a source relationship, not an instruction to run code.",
            "limitation": limit}


def path_to_label(nodes, edges, start_id, target_id, level, include_inferred, include_organization):
    return path_between_items(nodes, edges, start_id, target_id, level, include_inferred, include_organization, labeled=True)


def possible_callers(nodes, target_id, level, max_depth=1, limit=60, index=None):
    """Trace bounded possible callers using recorded call sites, never runtime traces."""
    if (level not in ("beginner", "explore", "technical") or type(max_depth) is not int or
            not 1 <= max_depth <= 3 or type(limit) is not int or not 1 <= limit <= 60):
        raise ValueError("invalid caller exploration")
    by_id = _nodes_by_id(index.nodes if index is not None else nodes)
    if target_id not in by_id or by_id[target_id].get("kind") != "function":
        raise ValueError("select a function in this scan")
    reverse, unresolved, partial = {}, 0, False
    if index is not None:
        # Read from every inferred call edge of the scan, so long call lists are not cut short.
        unresolved, partial = index.unresolved_calls, not index.complete

        class _Reverse:
            @staticmethod
            def get(target, default=()):
                return index.callers_of(target) or default
        reverse = _Reverse
    for caller in (() if index is not None else nodes):
        if caller.get("kind") not in ("file", "function"): continue
        partial = partial or bool(caller.get("call_sites_truncated"))
        for call in caller.get("call_sites", []):
            target = call.get("target_id")
            if call.get("resolution") == "unresolved": unresolved += 1
            if call.get("resolution") != "inferred" or call.get("evidence") != "static" or target not in by_id:
                continue
            via = {key: call.get(key) for key in ("written", "path", "line", "snippet", "snippet_truncated", "sha256", "basis")}
            via.update({"source": caller["id"], "target": target, "evidence": "inferred", "call_evidence": "static"})
            reverse.setdefault(target, []).append(via)
    for sites in (() if index is not None else reverse.values()):
        sites.sort(key=lambda item: (item.get("path") or "", item.get("line") or 0, item["source"], item.get("written") or ""))
    prompts = {"beginner": "Find the written call. A possible target does not prove the call ran.",
               "explore": "Compare the call with its possible target and consider what could change the match.",
               "technical": "Inspect the source, resolution basis and possible dynamic dispatch or rebinding."}
    queue, seen, rows, truncated = [(target_id, [])], {target_id}, [], False
    for target, chain in queue:
        for via in reverse.get(target, []):
            path = [via] + chain
            if len(rows) >= limit:
                truncated = True; break
            rows.append({"caller_id": via["source"], "hops": len(path), "path": path, "prompt": prompts[level]})
            if len(path) < max_depth and via["source"] not in seen:
                seen.add(via["source"]); queue.append((via["source"], path))
        if truncated: break
    return {"rows": rows, "truncated": truncated, "recorded_unresolved_calls": unresolved,
            "call_sites_truncated": partial,
            "limitation": "These are possible targets inferred from written calls, not observed runtime calls. "
                          "Unresolved and unscanned calls are not included; alternate paths may be omitted. "
                          "A call site's presence does not prove execution or that a change affects a caller."}


def guided_path(nodes, edges, start_id, goal, level, prefer_unreviewed, reviewed=None):
    """Build a bounded path from recorded graph edges without inventing execution.

    reviewed, when given, maps an item id to its current review mark; otherwise the items' own fields are used.
    """
    by_id = _nodes_by_id(nodes)
    if start_id not in by_id:
        raise ValueError("start item is not in this scan")

    class _Links:
        """Edges leaving or entering one item, limited to items in this scan."""
        def __init__(self, end):
            self.end, self.cache = end, {}

        def get(self, node_id, default=()):
            if node_id not in self.cache:
                self.cache[node_id] = [e for e in _incident(edges, node_id) if e[self.end] == node_id and
                                       e["source"] in by_id and e["target"] in by_id]
            return self.cache[node_id] or default
    outgoing, incoming = _Links("source"), _Links("target")

    def is_reviewed(node):
        if reviewed is not None:
            return bool(reviewed(node["id"]))
        return bool(node.get("reviewed") and not node.get("review_needs_refresh"))

    def rank(edge, direction):
        other = by_id[edge["target"] if direction == "forward" else edge["source"]]
        return (int(is_reviewed(other)) if prefer_unreviewed else 0, other["id"], edge["kind"], edge.get("line") or 0)

    def options(node_id):
        choices = []
        for edge in outgoing.get(node_id, []):
            if edge["kind"] in ("contains", "defines"):
                choices.append((edge, "forward"))
            elif goal == "dependencies" and edge["kind"] in ("imports", "includes"):
                choices.append((edge, "forward"))
        if goal in ("dependencies", "understand"):
            for edge in incoming.get(node_id, []):
                if edge["kind"] == "contains written call" or (goal == "dependencies" and edge["kind"] == "defines"):
                    choices.append((edge, "reverse"))
        return sorted(choices, key=lambda pair: rank(*pair))

    def finished(node_id):
        node = by_id[node_id]
        if goal == "understand":
            return node["kind"] == "function"
        if goal == "dependencies":
            return node["kind"] == "dependency"
        return node["kind"] in ("function", "file", "callsite") and (bool(node.get("call_sites")) or any(
            e["kind"] in ("possible call", "possible target") and e["evidence"] == "inferred" for e in outgoing.get(node_id, [])))

    queue = [(start_id, [])]
    seen = {start_id}
    matches = []
    shortest = None
    for node_id, path in queue:
        if shortest is not None and len(path) > shortest:
            break
        if len(seen) > MAX_PATH_SEARCH_ITEMS and matches:
            break
        if finished(node_id):
            shortest = len(path)
            matches.append((node_id, path))
            continue
        if len(path) >= 5 or shortest is not None:
            continue
        for edge, direction in options(node_id):
            other = edge["target"] if direction == "forward" else edge["source"]
            if other not in seen:
                seen.add(other)
                queue.append((other, path + [(edge, direction)]))
    if goal == "calls" or prefer_unreviewed:
        def match_rank(pair):
            node_id = pair[0]
            resolved = any(e["kind"] in ("possible call", "possible target") and e["evidence"] == "inferred" for e in outgoing.get(node_id, []))
            return (int(not resolved) if goal == "calls" else 0, int(is_reviewed(by_id[node_id])) if prefer_unreviewed else 0)
        matches.sort(key=match_rank)
    chosen = matches[0] if matches else None
    if chosen is None:
        return {"status": "unavailable", "goal": goal, "steps": [],
                "message": "No matching path was found from this item within the scanned graph. Try another starting item or goal.",
                "limitation": "Unscanned code, unresolved connections and runtime behavior are outside this result."}
    target, path = chosen
    steps = []
    def add(node_id, topic, via=None):
        prompts = {
            "beginner": {"overview": "Find this part and notice where it lives.", "inputs": "What names can receive values?", "returns": "Where does the source write return?", "structure": "Where does the code choose or repeat?", "calls": "What calls are written here?", "connections": "What does this link actually show?"},
            "explore": {"overview": "Describe this part together, using the source location.", "inputs": "Compare these inputs with a possible caller.", "returns": "Compare the return locations with the source.", "structure": "Find the choices and loops in the source together.", "calls": "Compare the written calls and discuss whether their targets are known.", "connections": "Follow the link and discuss what remains uncertain."},
            "technical": {"overview": "Inspect the definition and its source identity.", "inputs": "Inspect recorded parameter syntax and its evidence.", "returns": "Inspect explicit return sites and coverage limits.", "structure": "Inspect recorded control-flow syntax and coverage limits.", "calls": "Inspect syntactic call sites and their resolution status.", "connections": "Inspect edge direction, evidence class and resolution basis."},
        }
        steps.append({"node_id": node_id, "topic": topic, "prompt": prompts[level][topic],
                      "via": via, "explanation": explain_with_provider(by_id[node_id], edges, topic, level)})
    add(start_id, "overview")
    for edge, direction in path:
        other = edge["target"] if direction == "forward" else edge["source"]
        via = {k: edge.get(k) for k in ("source", "target", "kind", "evidence", "path", "line", "sha256", "snippet", "snippet_truncated", "basis")}
        via["direction"] = direction
        add(other, "overview", via)
    if goal == "understand":
        add(target, "inputs")
        if (by_id[target].get("shape") or {}).get("features"):
            add(target, "structure")
        if by_id[target].get("call_sites"):
            add(target, "calls")
        add(target, "returns")
    elif goal == "calls":
        choices = [e for e in outgoing.get(target, []) if e["kind"] in ("possible call", "possible target") and e["evidence"] == "inferred"]
        add(target, "calls")
        if choices:
            edge = sorted(choices, key=lambda e: rank(e, "forward"))[0]
            via = {k: edge.get(k) for k in ("source", "target", "kind", "evidence", "path", "line", "sha256", "snippet", "snippet_truncated", "basis")}
            via["direction"] = "forward"
            add(edge["target"], "overview", via)
    else:
        add(target, "connections")
        modules = [e for e in outgoing.get(target, []) if e["kind"] == "possible local module" and e["evidence"] == "inferred"]
        if modules:
            edge = sorted(modules, key=lambda e: rank(e, "forward"))[0]
            via = {k: edge.get(k) for k in ("source", "target", "kind", "evidence", "path", "line", "sha256", "snippet", "snippet_truncated", "basis")}
            via["direction"] = "forward"
            add(edge["target"], "overview", via)
    return {"status": "ready", "goal": goal, "steps": steps,
            "message": ("No target connection is available for this step in the current graph; inspect the written calls. " if goal == "calls" and not choices else "") + "Review marks may reorder equally near suggestions only when you choose that option; they are not test scores.",
            "limitation": "Possible calls and local targets are inferred, not observed. A path is not a runtime trace."}

"""Composition obligations (chapter 03): valid parts are not a valid whole.

A pipeline description (data, never code) lists activities, the artifacts they consume and produce, tags and generations.
The vocabulary is deliberately tiny: exact tag equality, finite membership, generation equality, explicit prerequisites and
explicitly declared unit-conversion activities. Nothing is inferred from names or familiar units, and ordering is not closure:
an acyclic graph with a missing input is still incomplete.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

from .graph import find_cycle, invalidation_paths, stable_topological_order

MAX_ACTIVITIES = 500
MAX_ITEMS = 50
# Exact, registered conversions: to_value = from_value * factor. A conversion is "verified" only if it matches this table.
CONVERSIONS = {("seconds", "milliseconds"): Decimal("1000"), ("milliseconds", "seconds"): Decimal("0.001"),
               ("minutes", "seconds"): Decimal("60"), ("milliseconds", "microseconds"): Decimal("1000"),
               ("microseconds", "milliseconds"): Decimal("0.001")}
V = ("SUPPORTED", "CONTRADICTED", "INSUFFICIENT_EVIDENCE", "INVALID_EVIDENCE")


class PipelineError(ValueError):
    """The description itself is malformed (INVALID_EVIDENCE), distinct from a failed obligation."""


def _is_str(x) -> bool:
    return isinstance(x, str) and bool(x)


def _tags(x, where) -> dict:
    if x is None:
        return {}
    if not isinstance(x, dict) or len(x) > MAX_ITEMS or not all(_is_str(k) and isinstance(v, (str, int)) and not isinstance(v, bool) for k, v in x.items()):
        raise PipelineError(f"{where}: tags must be an object of string/integer values")
    return x


def _requirement(v, where):
    if _is_str(v) or (isinstance(v, int) and not isinstance(v, bool)):
        return {"equals": v}
    if isinstance(v, dict) and len(v) == 1 and "equals" in v and _is_str(v["equals"]):
        return v
    if isinstance(v, dict) and set(v) == {"in"} and isinstance(v["in"], list) and v["in"] and len(v["in"]) <= MAX_ITEMS \
            and all(_is_str(x) for x in v["in"]):
        return v
    raise PipelineError(f"{where}: a requirement is a value, {{equals}} or {{in: [..]}}")


def parse_pipeline(obj) -> dict:
    """Validate shape and normalise. Raises PipelineError; never guesses."""
    if not isinstance(obj, dict) or set(obj) - {"external", "activities", "same_generation"}:
        raise PipelineError("pipeline must be an object with external, activities, same_generation only")
    acts_raw = obj.get("activities")
    if not isinstance(acts_raw, list) or not acts_raw or len(acts_raw) > MAX_ACTIVITIES:
        raise PipelineError("activities must be a non-empty list")
    ext = []
    for i, e in enumerate(obj.get("external", [])):
        if not isinstance(e, dict) or set(e) - {"artifact", "tags", "generation", "assumption"} or not _is_str(e.get("artifact")):
            raise PipelineError(f"external[{i}] needs an artifact name")
        ext.append({"artifact": e["artifact"], "tags": _tags(e.get("tags"), f"external[{i}]"),
                    "generation": e.get("generation"), "assumption": e.get("assumption")})
    acts, seen = [], set()
    for i, a in enumerate(acts_raw):
        if not isinstance(a, dict) or set(a) - {"id", "consumes", "produces", "depends_on", "conversion"} or not _is_str(a.get("id")):
            raise PipelineError(f"activities[{i}] needs an id and only known fields")
        if a["id"] in seen:
            raise PipelineError(f"duplicate activity id {a['id']!r}")
        seen.add(a["id"])
        cons = []
        for j, c in enumerate(a.get("consumes", [])):
            if not isinstance(c, dict) or set(c) - {"artifact", "requires"} or not _is_str(c.get("artifact")):
                raise PipelineError(f"activities[{i}].consumes[{j}] needs an artifact")
            req = c.get("requires", {})
            if not isinstance(req, dict) or len(req) > MAX_ITEMS:
                raise PipelineError("requires must be an object")
            cons.append({"artifact": c["artifact"], "requires": {k: _requirement(v, f"requires.{k}") for k, v in req.items()}})
        prods = []
        for j, p in enumerate(a.get("produces", [])):
            if not isinstance(p, dict) or set(p) - {"artifact", "tags", "generation"} or not _is_str(p.get("artifact")):
                raise PipelineError(f"activities[{i}].produces[{j}] needs an artifact")
            prods.append({"artifact": p["artifact"], "tags": _tags(p.get("tags"), "produces"), "generation": p.get("generation")})
        dep = a.get("depends_on", [])
        if not isinstance(dep, list) or not all(_is_str(d) for d in dep) or len(dep) != len(set(dep)):
            raise PipelineError(f"activities[{i}].depends_on must be a list of distinct ids")
        conv = a.get("conversion")
        if conv is not None and (not isinstance(conv, dict) or set(conv) != {"from", "to", "factor"} or not all(_is_str(conv[k]) for k in conv)):
            raise PipelineError(f"activities[{i}].conversion needs from, to and a decimal factor string")
        acts.append({"id": a["id"], "consumes": cons, "produces": prods, "depends_on": dep, "conversion": conv})
    sg = obj.get("same_generation", [])
    if not isinstance(sg, list) or not all(isinstance(g, list) and len(g) >= 2 and all(_is_str(x) for x in g) for g in sg):
        raise PipelineError("same_generation must be a list of artifact-name lists (length >= 2)")
    return {"external": ext, "activities": acts, "same_generation": sg}


def _producers(p) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for a in p["activities"]:
        for pr in a["produces"]:
            out.setdefault(pr["artifact"], []).append(a["id"])
    return out


def edges_of(p) -> list[tuple[str, str]]:
    ids = {a["id"] for a in p["activities"]}
    prod = _producers(p)
    edges = set()
    for a in p["activities"]:
        for d in a["depends_on"]:
            if d in ids and d != a["id"]:
                edges.add((d, a["id"]))
        for c in a["consumes"]:
            for src in prod.get(c["artifact"], []):
                if src != a["id"]:
                    edges.add((src, a["id"]))
    return sorted(edges)


def _closure(p, target: str) -> set[str]:
    rev: dict[str, set[str]] = {}
    for a, b in edges_of(p):
        rev.setdefault(b, set()).add(a)
    seen, stack = {target}, [target]
    while stack:
        n = stack.pop()
        for m in sorted(rev.get(n, ())):
            if m not in seen:
                seen.add(m)
                stack.append(m)
    return seen


def _meets(req: dict, value) -> bool:
    return value == req["equals"] if "equals" in req else value in req["in"]


def analyze(p: dict, target: str | None = None) -> dict:
    """Evaluate every declared obligation separately and keep each finding. p must come from parse_pipeline."""
    ids = {a["id"] for a in p["activities"]}
    by_id = {a["id"]: a for a in p["activities"]}
    if target is not None and target not in ids:
        raise PipelineError(f"target {target!r} is not an activity")
    scope = _closure(p, target) if target else set(ids)
    prod, edges = _producers(p), edges_of(p)
    ext = {e["artifact"]: e for e in p["external"]}
    obligations: list[dict] = []
    assumptions: list[str] = []

    def ob(oid, typ, verdict, reason, witness=None):
        obligations.append({"id": oid, "type": typ, "verdict": verdict, "reason": reason, "witness": witness or {}})

    # ---- structure: references, collisions, conflicting declarations
    problems = []
    for art, who in sorted(prod.items()):
        if len(who) > 1 and any(w in scope for w in who):
            problems.append({"code": "producer_collision", "artifact": art, "activities": sorted(who)})
        if art in ext:
            problems.append({"code": "internal_and_external", "artifact": art})
    if problems:
        ob("graph:declarations", "structure", "INVALID_EVIDENCE", problems[0]["code"], {"problems": problems})
    missing_refs = sorted({(a["id"], d) for a in p["activities"] if a["id"] in scope for d in a["depends_on"] if d not in ids})
    selfdep = sorted(a["id"] for a in p["activities"] if a["id"] in scope and a["id"] in a["depends_on"])
    if selfdep:
        ob("graph:self_dependency", "structure", "INVALID_EVIDENCE", "self_dependency", {"activities": selfdep})
    if missing_refs:
        ob("graph:references", "prerequisite", "INSUFFICIENT_EVIDENCE", "prerequisite_not_declared",
           {"missing": [{"activity": a, "prerequisite": d} for a, d in missing_refs]})
    else:
        ob("graph:references", "prerequisite", "SUPPORTED", "prerequisites_declared")
    # ---- ordering (separate from closure)
    sub_edges = [(a, b) for a, b in edges if a in scope and b in scope]
    order, residual = stable_topological_order(scope, sub_edges)
    cycle, trunc = find_cycle(residual, sub_edges) if residual else (None, False)
    if cycle:
        ob("graph:acyclic", "ordering", "CONTRADICTED", "cycle", {"cycle": list(cycle), "truncated": trunc})
    else:
        ob("graph:acyclic", "ordering", "SUPPORTED", "acyclic", {"order": list(order)})
    # ---- conversions: verified only against the registered exact table
    for a in sorted(p["activities"], key=lambda x: x["id"]):
        if a["id"] not in scope or not a["conversion"]:
            continue
        c, oid = a["conversion"], f"conversion:{a['id']}"
        known = CONVERSIONS.get((c["from"], c["to"]))
        try:
            factor = Decimal(c["factor"])
            ok_num = factor.is_finite()
        except InvalidOperation:
            ok_num = False
        in_tags = [x["requires"].get("unit") for x in a["consumes"]]
        out_units = [x["tags"].get("unit") for x in a["produces"]]
        if not ok_num:
            ob(oid, "conversion", "INVALID_EVIDENCE", "factor_not_decimal", {"declared": c})
        elif known is None:
            ob(oid, "conversion", "INSUFFICIENT_EVIDENCE", "conversion_not_verifiable",
               {"declared": c, "note": "no exact registered conversion for this pair; none is inferred from familiar unit names"})
        elif factor != known:
            ob(oid, "conversion", "CONTRADICTED", "conversion_factor_wrong", {"declared": c, "registered_factor": str(known)})
        elif not any(u is not None and _meets(u, c["from"]) for u in in_tags) or c["to"] not in out_units:
            ob(oid, "conversion", "CONTRADICTED", "conversion_tags_inconsistent",
               {"declared": c, "consumes_unit": [u for u in in_tags], "produces_unit": out_units})
        else:
            ob(oid, "conversion", "SUPPORTED", "conversion_verified", {"declared": c})
    # ---- per-edge obligations
    for a in sorted(p["activities"], key=lambda x: x["id"]):
        if a["id"] not in scope:
            continue
        for c in sorted(a["consumes"], key=lambda x: x["artifact"]):
            oid = f"input:{a['id']}:{c['artifact']}"
            who = prod.get(c["artifact"], [])
            if len(who) > 1:
                continue                                          # reported by graph:declarations
            if who:
                src = by_id[who[0]]
                g = next(x for x in src["produces"] if x["artifact"] == c["artifact"])
                guarantee, origin, kind = g["tags"], who[0], "producer"
            elif c["artifact"] in ext:
                e = ext[c["artifact"]]
                if not e["assumption"]:
                    ob(oid, "edge", "INSUFFICIENT_EVIDENCE", "external_without_assumption", {"artifact": c["artifact"]})
                    continue
                guarantee, origin, kind = e["tags"], "external", "external"
                assumptions.append(f"external input '{c['artifact']}' assumed: {e['assumption']}")
            else:
                ob(oid, "edge", "INSUFFICIENT_EVIDENCE", "undeclared_input",
                   {"artifact": c["artifact"], "consumer": a["id"], "note": "no producer and no declared external source"})
                continue
            fails, unknown, checked = [], [], []
            for tag, req in sorted(c["requires"].items()):
                if tag not in guarantee:
                    unknown.append({"tag": tag, "requirement": req})
                elif not _meets(req, guarantee[tag]):
                    fails.append({"tag": tag, "requirement": req, "guarantee": guarantee[tag]})
                else:
                    checked.append(tag)
            w = {"producer": origin, "consumer": a["id"], "artifact": c["artifact"], "source": kind}
            if fails:
                ob(oid, "edge", "CONTRADICTED", "tag_mismatch", {**w, "mismatches": fails})
            elif unknown:
                ob(oid, "edge", "INSUFFICIENT_EVIDENCE", "tag_not_established", {**w, "not_established": unknown})
            else:
                ob(oid, "edge", "SUPPORTED", "requirements_met", {**w, "checked_tags": checked})
            if kind == "producer" and not fails and not unknown and c["requires"]:
                assumptions.append(f"tags declared by activity '{origin}' for '{c['artifact']}' are producer assertions, not independently verified")
    # ---- generation equality
    for i, group in enumerate(p["same_generation"]):
        if not all(g in prod or g in ext for g in group):
            ob(f"generation:{i}", "generation", "INSUFFICIENT_EVIDENCE", "artifact_not_declared", {"artifacts": group})
            continue
        if not any((prod.get(g) or [None])[0] in scope or g in ext for g in group):
            continue
        gens = {}
        for g in group:
            if g in prod and len(prod[g]) == 1:
                gens[g] = next(x for x in by_id[prod[g][0]]["produces"] if x["artifact"] == g)["generation"]
            elif g in ext:
                gens[g] = ext[g]["generation"]
        if len(gens) < len(group) or any(v is None for v in gens.values()):
            ob(f"generation:{i}", "generation", "INSUFFICIENT_EVIDENCE", "generation_unstated", {"generations": gens})
        elif len(set(gens.values())) > 1:
            ob(f"generation:{i}", "generation", "CONTRADICTED", "generations_differ", {"generations": gens})
        else:
            ob(f"generation:{i}", "generation", "SUPPORTED", "generations_equal", {"generations": gens})
    verdicts = [o["verdict"] for o in obligations]
    overall = ("CONTRADICTED" if "CONTRADICTED" in verdicts else "INVALID_EVIDENCE" if "INVALID_EVIDENCE" in verdicts
               else "INSUFFICIENT_EVIDENCE" if "INSUFFICIENT_EVIDENCE" in verdicts else "SUPPORTED")
    return {"overall": overall, "obligations": obligations, "order": list(order) if not cycle else [], "edges": [list(e) for e in sub_edges],
            "conditional_on": sorted(set(assumptions)) if overall == "SUPPORTED" else sorted(set(assumptions)),
            "scope": {"target": target, "evaluated": sorted(scope), "not_evaluated": sorted(ids - scope),
                      "statement": ("Only the ancestors of the target were evaluated; other branches are unresolved, not verified."
                                    if target else "Every activity was evaluated.")}}


def impact(old: dict, new: dict) -> dict:
    """What changed between two pipeline versions, and which activities a change reaches (with a witness path each)."""
    oa = {a["id"]: a for a in old["activities"]}
    na = {a["id"]: a for a in new["activities"]}
    changed = sorted(i for i in oa.keys() & na.keys() if oa[i] != na[i])
    added, removed = sorted(na.keys() - oa.keys()), sorted(oa.keys() - na.keys())
    ext_changed = sorted(a for a in ({e["artifact"] for e in old["external"]} | {e["artifact"] for e in new["external"]})
                         if next((e for e in old["external"] if e["artifact"] == a), None) != next((e for e in new["external"] if e["artifact"] == a), None))
    edges = sorted(set(edges_of(old)) | set(edges_of(new)))
    roots = changed + added
    for art in ext_changed:                                   # a changed external input affects its consumers directly
        roots += sorted(a["id"] for a in new["activities"] if any(c["artifact"] == art for c in a["consumes"]))
    affected: dict[str, list[str]] = {}
    for r in sorted(set(roots)):
        affected.setdefault(r, [r])
        for node, path in invalidation_paths(edges, r).items():
            affected.setdefault(node, list(path))
    return {"changed": changed, "added": added, "removed": removed, "external_changed": ext_changed,
            "affected": {k: v for k, v in sorted(affected.items())},
            "unaffected": sorted(set(na) - set(affected)),
            "note": "Each affected activity carries a path from the change. Unaffected branches keep their earlier results."}

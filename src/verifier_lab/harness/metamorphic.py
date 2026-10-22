"""Metamorphic relations (chapter 09). Each has a precondition and a stated reason it is sound for these rules.

Deliberately absent: "more evidence never hurts" and "any event permutation is neutral" - neither is a valid law.
"""
from __future__ import annotations

import json
import random
from dataclasses import dataclass
from typing import Callable

from ..canonical import record_id
from .world import ClaimSpec, Doc, Outcome, Scenario

STRONG = ("SUPPORTED", "CONTRADICTED")      # conclusions a rule may only reach from sufficient evidence


@dataclass(frozen=True)
class RelationSpec:
    relation_id: str
    soundness: str
    precondition: Callable[[Scenario], bool]
    transform: Callable[[Scenario, random.Random], Scenario]
    check: Callable[[Scenario, dict[str, Outcome], dict[str, Outcome]], tuple[bool, str]]


def _all_json(s: Scenario) -> bool:
    ok = True
    for d in (s.origin, *s.evidence):
        if d.fmt != "json":
            continue
        try:
            json.loads(d.data)
        except ValueError:
            ok = False
    return ok and any(d.fmt == "json" for d in s.evidence)


def _reverse_keys(obj):
    if isinstance(obj, dict):
        return {k: _reverse_keys(obj[k]) for k in reversed(list(obj))}
    if isinstance(obj, list):
        return [_reverse_keys(x) for x in obj]
    return obj


def _same(base, new) -> tuple[bool, str]:
    if base == new:
        return True, ""
    diff = {k: (base.get(k), new.get(k)) for k in set(base) | set(new) if base.get(k) != new.get(k)}
    return False, f"findings changed: {diff}"


# 1. object key order is not semantic
def _t_reorder(s, rng):
    ev = tuple(Doc(d.name, d.fmt, json.dumps(_reverse_keys(json.loads(d.data)), indent=1).encode()) if d.fmt == "json" else d
               for d in s.evidence)
    return Scenario(s.id, s.origin, ev, s.claims)


def _c_reorder(s, base, new):
    ok, why = _same(base, new)
    if not ok:
        return ok, why
    for d in s.evidence:       # the canonical identity of every parsed JSON document is unchanged as well
        if d.fmt == "json" and record_id("doc", json.loads(d.data)) != record_id("doc", _reverse_keys(json.loads(d.data))):
            return False, "canonical identity changed under key reordering"
    return True, ""


# 2. bijective renaming of opaque identifiers (with references rebuilt)
def _rename_obj(obj, m):
    if isinstance(obj, dict):
        return {m.get(k, k): _rename_obj(v, m) for k, v in obj.items()}
    return obj


def _t_rename(s, rng):
    names: list[str] = []

    def collect(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k not in names:
                    names.append(k)
                collect(v)
    for d in s.evidence:
        collect(json.loads(d.data))
    reserved = {"start", "end", "start_min", "start_max", "end_min", "end_max"}   # predicate vocabulary is not opaque
    m = {k: f"r{n}" for n, k in enumerate(k for k in names if k not in reserved)}
    m = {k: v for k, v in zip([k for k in names if k not in reserved], m.values())}

    def ren_key(key):
        return ".".join(m.get(p, p) for p in key.split("."))
    claims = []
    for c in s.claims:
        args = dict(c.arguments)
        if c.predicate == "interval.relation":
            args["left"], args["right"] = m.get(args["left"], args["left"]), m.get(args["right"], args["right"])
        claims.append(ClaimSpec(c.key, c.predicate, ren_key(c.subject) if c.predicate != "interval.relation" else c.subject, args))
    ev = tuple(Doc(d.name, d.fmt, json.dumps(_rename_obj(json.loads(d.data), m), indent=1).encode()) for d in s.evidence)
    return Scenario(s.id, s.origin, ev, tuple(claims))


# 3. a disconnected, irrelevant artifact does not strengthen or change anything
def _t_irrelevant(s, rng):
    extra = Doc("unrelated.json", "json", json.dumps({"zz_unrelated": rng.randint(0, 9)}).encode())
    return Scenario(s.id, s.origin, s.evidence + (extra,), s.claims)


# 4. removing the only witness cannot leave a claim SUPPORTED
def _p_single_witness(s):
    return len(s.evidence) == 1 and _all_json(s)


def _t_drop_witness(s, rng):
    return Scenario(s.id, s.origin, (), s.claims)


def _c_not_supported(s, base, new):
    bad = [k for k, (v, _) in new.items() if v == "SUPPORTED"]
    return (not bad), f"still SUPPORTED without any evidence: {bad}"


# 5. increasing uncertainty must not create a stronger temporal conclusion
def _p_interval(s):
    return any(c.predicate == "interval.relation" for c in s.claims) and _all_json(s) and len(s.evidence) == 1


def _t_widen(s, rng):
    d = s.evidence[0]
    obj = json.loads(d.data)
    for name, rec in list(obj.items()):
        if isinstance(rec, dict) and "start" in rec and "end" in rec:
            s0, e0 = rec.pop("start"), rec.pop("end")
            rec["start_min"], rec["start_max"] = s0 - 100, s0 + 100
            rec["end_min"], rec["end_max"] = e0 - 100, e0 + 100
    return Scenario(s.id, s.origin, (Doc(d.name, d.fmt, json.dumps(obj, indent=1).encode()),), s.claims)


def _c_monotone(s, base, new):
    bad = {k: (base[k], new[k]) for k in new if new[k][0] in STRONG and new[k][0] != base[k][0]}
    return (not bad), f"stronger conclusion after widening uncertainty: {bad}"


# 6. claim and evidence order do not matter
def _t_order(s, rng):
    claims, ev = list(s.claims), list(s.evidence)
    rng.shuffle(claims)
    rng.shuffle(ev)
    return Scenario(s.id, s.origin, tuple(ev), tuple(claims))


RELATIONS: tuple[RelationSpec, ...] = (
    RelationSpec("reorder_json_keys", "JSON objects are unordered; canonical identity ignores member order.",
                 _all_json, _t_reorder, _c_reorder),
    RelationSpec("rename_opaque_identifiers", "Fact and activity names are opaque; a bijective rename with rebuilt references preserves meaning (predicate vocabulary such as start/end is not renamed).",
                 _all_json, _t_rename, lambda s, b, n: _same(b, n)),
    RelationSpec("add_irrelevant_artifact", "Evidence about an unrelated fact cannot change a claim about another fact.",
                 _all_json, _t_irrelevant, lambda s, b, n: _same(b, n)),
    RelationSpec("remove_only_witness", "With no evidence at all nothing can be SUPPORTED under the same rule.",
                 _p_single_witness, _t_drop_witness, _c_not_supported),
    RelationSpec("increase_uncertainty", "Wider possibility intervals can only weaken a definite temporal conclusion.",
                 _p_interval, _t_widen, _c_monotone),
    RelationSpec("reorder_claims_and_evidence", "Findings are keyed by claim identity; processing order is not semantic.",
                 lambda s: True, _t_order, lambda s, b, n: _same(b, n)),
)
RELATION_BY_ID = {r.relation_id: r for r in RELATIONS}

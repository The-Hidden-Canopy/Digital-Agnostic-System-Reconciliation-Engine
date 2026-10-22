#!/usr/bin/env python3
"""Example verifier program for `verifier-lab campaign run --kernel-cmd`.

A small, standalone verifier for the fact predicates only (fact.equals, fact.numeric_bound, fact.present) over JSON evidence.
It deliberately does NOT implement interval.relation, so a campaign shows what a partial verifier looks like: it is judged
only by what it answers. Set VERIFIER_DEFECTS=last_key_wins to give it a classic bug (duplicate keys silently resolved).
Standard library only - copy it as the starting point for your own verifier.
"""
import json
import os
import sys
from decimal import Decimal

DEFECTS = set(filter(None, os.environ.get("VERIFIER_DEFECTS", "").split(",")))


def flat(obj, prefix=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from flat(v, f"{prefix}.{k}" if prefix else k)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from flat(v, f"{prefix}[{i}]")
    else:
        yield prefix, obj


def kind(v):
    return "null" if v is None else "boolean" if isinstance(v, bool) else "integer" if isinstance(v, int) else "decimal" if isinstance(v, float) else "string"


def load(doc):
    def pairs(p):
        keys = [k for k, _ in p]
        if len(keys) != len(set(keys)) and "last_key_wins" not in DEFECTS:
            raise ValueError("duplicate key")
        return dict(p)
    return json.loads(doc["text"], object_pairs_hook=pairs)


def judge(scn, claim):
    if claim["predicate"] not in ("fact.equals", "fact.numeric_bound", "fact.present"):
        return ["INSUFFICIENT_EVIDENCE", "no_registered_rule"]
    values, bad = [], 0
    for doc in scn["evidence"]:
        if doc["name"] == scn["origin"]["name"] and doc["text"] == scn["origin"]["text"]:
            continue                                       # a claim's own source is never evidence for it
        try:
            facts = dict(flat(load(doc)))
        except ValueError:
            bad += 1
            continue
        if claim["subject"] in facts:
            values.append(facts[claim["subject"]])
    if not values:
        return ["INVALID_EVIDENCE", "evidence_unreadable"] if bad and bad == len(scn["evidence"]) else ["INSUFFICIENT_EVIDENCE", "fact_not_found"]
    if claim["predicate"] == "fact.present":
        return ["SUPPORTED", "fact_present"]
    a = claim["arguments"]
    if claim["predicate"] == "fact.equals":
        exp = a["expected"]
        res = []
        for v in values:
            if kind(v) in ("integer", "decimal") and exp["kind"] in ("integer", "decimal"):
                res.append(Decimal(str(v)) == Decimal(exp["value"]))
            elif kind(v) != exp["kind"]:
                return ["HUMAN_REVIEW_REQUIRED", "kind_mismatch"]
            else:
                res.append(v == exp["value"])
    else:
        if not all(kind(v) in ("integer", "decimal") for v in values):
            return ["HUMAN_REVIEW_REQUIRED", "kind_mismatch"]
        b, op = Decimal(a["bound"]["value"]), a["op"]
        res = [{"lt": Decimal(str(v)) < b, "lte": Decimal(str(v)) <= b, "gt": Decimal(str(v)) > b, "gte": Decimal(str(v)) >= b}[op] for v in values]
    if all(res) and not bad:
        return ["SUPPORTED", "value_equal" if claim["predicate"] == "fact.equals" else "bound_satisfied"]
    if not any(res):
        return ["CONTRADICTED", "value_mismatch" if claim["predicate"] == "fact.equals" else "bound_violated"]
    return ["HUMAN_REVIEW_REQUIRED", "evidence_conflict" if not all(res) else "evidence_source_unreadable"]


def main():
    for line in sys.stdin:
        req = json.loads(line)
        if req.get("cmd") != "verify":
            print(json.dumps({"ok": False, "error": "unsupported"}), flush=True)
            continue
        scn = req["scenario"]
        print(json.dumps({"ok": True, "outcomes": {c["key"]: judge(scn, c) for c in scn["claims"]}}), flush=True)


if __name__ == "__main__":
    main()

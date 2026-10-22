"""Campaigns (chapters 09 and 10): a fixed, digest-pinned case manifest, execution against a kernel, and a report whose
totals are recomputable from its cases.

Statuses are a campaign vocabulary and never domain verdicts: PASSED, FAILED, SKIPPED_PRECONDITION, ERROR.
"""
from __future__ import annotations

import random

from ..canonical import canonical_bytes, sha256_digest
from .metamorphic import RELATIONS, RELATION_BY_ID
from .mutation import GENERATOR_VERSION, OPERATOR_BY_ID, OPERATORS, mutate
from .world import BASE_EXPECTED, Kernel, KERNELS, Scenario, base_scenarios

MANIFEST_SCHEMA = "verifier-lab/campaign-manifest/1"
REPORT_SCHEMA = "verifier-lab/campaign-report/1"
STATUSES = ("PASSED", "FAILED", "SKIPPED_PRECONDITION", "ERROR")
DOMAIN_VERDICTS = ("SUPPORTED", "CONTRADICTED", "INSUFFICIENT_EVIDENCE", "INVALID_EVIDENCE", "NOT_APPLICABLE", "HUMAN_REVIEW_REQUIRED")


def _digest(obj) -> str:
    return sha256_digest(canonical_bytes(obj))


def build_manifest(seed: str = "campaign-1") -> dict:
    """Cases are enumerated in a fixed order: base x claim x operator, then base x relation."""
    cases = []
    for s in base_scenarios():
        for c in s.claims:
            for op in OPERATORS:
                holds = op.applies(s, c)
                exp_v, exp_r = op.expected(c)
                touched_probe = set(c.touch_keys())
                case = {"id": f"mut:{op.id}:{s.id}:{c.key}", "layer": op.layer, "kind": "mutation", "base": s.id,
                        "claim": c.key, "predicate": c.predicate, "operator": op.id, "seed": seed,
                        "precondition_holds": holds, "expected": {"verdict": exp_v, "reason": exp_r},
                        "protected_base_verdict": BASE_EXPECTED, "affects_all": op.affects_all}
                cases.append(case)
        for rel in RELATIONS:
            cases.append({"id": f"meta:{rel.relation_id}:{s.id}", "layer": "metamorphic", "kind": "relation",
                          "base": s.id, "relation": rel.relation_id, "seed": seed,
                          "precondition_holds": rel.precondition(s), "expected": {"relation": "holds"}})
    cases = [{**c, "digest": _digest(c)} for c in cases]
    body = {"schema": MANIFEST_SCHEMA, "generator": {"version": GENERATOR_VERSION, "algorithm": "python-random-seeded-per-case",
                                                      "seed": seed, "note": "testing convenience, not cryptography"},
            "bases": {s.id: s.digest() for s in base_scenarios()}, "cases": cases}
    return {**body, "manifest_digest": _digest(body)}


def _valid_verdict(v) -> bool:
    return v in DOMAIN_VERDICTS


def run_case(case: dict, kernel: Kernel, bases: dict[str, Scenario]) -> dict:
    out = {"id": case["id"], "layer": case["layer"], "kind": case["kind"], "expected": case["expected"],
           "digest": case["digest"], "status": "ERROR", "comparison": None, "mutation": None, "error": None}
    if case["kind"] == "mutation":
        out["operator"], out["predicate"], out["claim"] = case["operator"], case["predicate"], case["claim"]
    else:
        out["relation"] = case["relation"]
    try:
        scenario = bases[case["base"]]
        if scenario.digest() != case.get("base_digest", scenario.digest()):
            raise RuntimeError("base scenario digest changed")
        if case["kind"] == "mutation":
            op = OPERATOR_BY_ID[case["operator"]]
            holds = op.applies(scenario, scenario.claim(case["claim"]))
            if holds != case["precondition_holds"]:
                raise RuntimeError("precondition disagrees with the committed manifest")
            if not holds:
                out["status"] = "SKIPPED_PRECONDITION"
                return out
            m = mutate(scenario, case["claim"], case["operator"], case["seed"])
            actual = kernel.verify(m.scenario)
            exp = (case["expected"]["verdict"], case["expected"]["reason"])
            target_actual = actual.get(case["claim"], (None, None))
            comp = {"target": {"claim": case["claim"], "expected": list(exp), "actual": list(target_actual),
                               "ok": tuple(target_actual) == exp}, "protected": [], "collateral": []}
            for c in scenario.claims:
                if c.key == case["claim"]:
                    continue
                a = actual.get(c.key, (None, None))
                if set(c.touch_keys()) & m.touched:
                    comp["collateral"].append({"claim": c.key, "actual": list(a), "valid": _valid_verdict(a[0])})
                else:
                    comp["protected"].append({"claim": c.key, "expected": [BASE_EXPECTED, None], "actual": list(a),
                                              "ok": a[0] == BASE_EXPECTED})
            ok = comp["target"]["ok"] and all(p["ok"] for p in comp["protected"]) and all(x["valid"] for x in comp["collateral"])
            out["comparison"], out["mutation"], out["status"] = comp, m.record.to_dict(), "PASSED" if ok else "FAILED"
        else:
            rel = RELATION_BY_ID[case["relation"]]
            holds = rel.precondition(scenario)
            if holds != case["precondition_holds"]:
                raise RuntimeError("precondition disagrees with the committed manifest")
            if not holds:
                out["status"] = "SKIPPED_PRECONDITION"
                return out
            base_out = kernel.verify(scenario)
            rng = random.Random(f"{case['seed']}|{case['id']}")
            changed = rel.transform(scenario, rng)
            new_out = kernel.verify(changed)
            ok, detail = rel.check(scenario, base_out, new_out)
            out["comparison"] = {"relation": {"ok": bool(ok), "detail": "" if ok else detail[:300]}}
            out["mutation"] = {"base_digest": scenario.digest(), "result_digest": changed.digest(),
                               "generator_version": GENERATOR_VERSION, "seed": case["seed"], "applied": {"relation": rel.relation_id}}
            out["status"] = "PASSED" if ok else "FAILED"
    except Exception as exc:       # an execution problem is its own status, never a pass or a domain verdict
        out["status"], out["error"] = "ERROR", f"{type(exc).__name__}: {exc}"[:300]
    return out


def summarize(cases: list[dict]) -> dict:
    """Recomputable from the cases alone: nothing here is trusted from the producer."""
    counts = {s: 0 for s in STATUSES}
    by_layer: dict[str, dict] = {}
    ops: dict[str, dict] = {}
    rules: dict[str, dict] = {}
    rels: dict[str, dict] = {}

    def bump(table, key, status):
        t = table.setdefault(key, {"cases": 0, "executed": 0, "passed": 0, "failed": 0, "skipped": 0, "errors": 0})
        t["cases"] += 1
        if status == "SKIPPED_PRECONDITION":
            t["skipped"] += 1
        elif status == "ERROR":
            t["errors"] += 1
        else:
            t["executed"] += 1
            t["passed" if status == "PASSED" else "failed"] += 1
    for c in cases:
        counts[c["status"]] += 1
        bump(by_layer, c["layer"], c["status"])
        if c["kind"] == "mutation":
            bump(ops, c["operator"], c["status"])
            bump(rules, c["predicate"], c["status"])
        else:
            bump(rels, c["relation"], c["status"])
    mutation = [c for c in cases if c["kind"] == "mutation" and c["status"] in ("PASSED", "FAILED")]
    detected = sum(1 for c in mutation if c["status"] == "PASSED")
    return {"counts": counts, "total_cases": len(cases), "by_layer": by_layer, "operators": ops, "rules": rules,
            "relations": rels,
            "never_executed": sorted([k for k, v in {**ops, **rels}.items() if v["executed"] == 0]),
            "mutation_detection": {"detected": detected, "executed_mutations": len(mutation),
                                   "scope": "among executed mutation cases on these synthetic bases only; not a universal safety score"}}


def run_campaign(kernel: Kernel | str = "real", manifest: dict | None = None, seed: str = "campaign-1") -> dict:
    kernel = KERNELS[kernel] if isinstance(kernel, str) else kernel
    manifest = manifest or build_manifest(seed)
    bases = {s.id: s for s in base_scenarios()}
    results = [run_case(c, kernel, bases) for c in manifest["cases"]]
    # a kernel that cannot even reproduce the committed base expectation is reported, not hidden
    base_check = {}
    for s in bases.values():
        try:
            got = kernel.verify(s)
            base_check[s.id] = all(v[0] == BASE_EXPECTED for v in got.values()) and set(got) == {c.key for c in s.claims}
        except Exception:
            base_check[s.id] = False
    summary = summarize(results)
    body = {"schema": REPORT_SCHEMA, "manifest_digest": manifest["manifest_digest"], "kernel": {"name": kernel.name},
            "base_expectations_reproduced": base_check, "cases": results, "summary": summary,
            "survivors": sorted(c["id"] for c in results if c["kind"] == "mutation" and c["status"] == "FAILED")}
    return {**body, "report_digest": _digest(body)}

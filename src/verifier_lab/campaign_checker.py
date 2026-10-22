"""Independent campaign-report checker (chapter 10). Imports NOTHING from the producer.

It checks report integrity against a manifest, and - only when the caller supplies a manifest digest from a trusted
channel - that this is the *intended* campaign. Without that anchor an attacker can drop a test and recompute both
manifest and report consistently, so the conclusion is labelled narrower. It never re-runs the kernel.
"""
from __future__ import annotations

import hashlib
import json
import sys

STATUSES = ("PASSED", "FAILED", "SKIPPED_PRECONDITION", "ERROR")
VERDICTS = ("SUPPORTED", "CONTRADICTED", "INSUFFICIENT_EVIDENCE", "INVALID_EVIDENCE", "NOT_APPLICABLE", "HUMAN_REVIEW_REQUIRED")
MANIFEST_SCHEMA = "verifier-lab/campaign-manifest/1"
REPORT_SCHEMA = "verifier-lab/campaign-report/1"


def _canon(v) -> bytes:
    return json.dumps(v, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _dg(v) -> str:
    return "sha256:" + hashlib.sha256(_canon(v)).hexdigest()


def _summarize(cases):
    counts = {s: 0 for s in STATUSES}
    layers, ops, rules, rels = {}, {}, {}, {}

    def bump(t, k, st):
        e = t.setdefault(k, {"cases": 0, "executed": 0, "passed": 0, "failed": 0, "skipped": 0, "errors": 0})
        e["cases"] += 1
        if st == "SKIPPED_PRECONDITION":
            e["skipped"] += 1
        elif st == "ERROR":
            e["errors"] += 1
        else:
            e["executed"] += 1
            e["passed" if st == "PASSED" else "failed"] += 1
    for c in cases:
        counts[c["status"]] += 1
        bump(layers, c["layer"], c["status"])
        if c["kind"] == "mutation":
            bump(ops, c["operator"], c["status"])
            bump(rules, c["predicate"], c["status"])
        else:
            bump(rels, c["relation"], c["status"])
    ex = [c for c in cases if c["kind"] == "mutation" and c["status"] in ("PASSED", "FAILED")]
    return {"counts": counts, "total_cases": len(cases), "by_layer": layers, "operators": ops, "rules": rules, "relations": rels,
            "never_executed": sorted([k for k, v in {**ops, **rels}.items() if v["executed"] == 0]),
            "mutation_detection": {"detected": sum(1 for c in ex if c["status"] == "PASSED"), "executed_mutations": len(ex),
                                   "scope": "among executed mutation cases on these synthetic bases only; not a universal safety score"}}


def verify_campaign_report(report: dict, manifest: dict, trusted_manifest_digest: str | None = None) -> dict:
    checks = []

    def check(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        return ok
    try:
        _verify(report, manifest, trusted_manifest_digest, check)
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        check("report_well_formed", False, f"{type(exc).__name__}: {exc}")
    ok = all(c["ok"] for c in checks)
    anchored = trusted_manifest_digest is not None
    scope = ("report is consistent with the trusted campaign manifest" if anchored and ok else
             "report is internally consistent with the supplied manifest; no trusted manifest digest was given, so this does NOT show "
             "the manifest is the intended campaign" if ok else "report rejected")
    return {"status": "ACCEPTED" if ok else "REJECTED", "anchored": anchored, "checks": checks, "conclusion": scope,
            "note": "A correctly hashed report can still document failing cases; see summary.counts and survivors."}


def _verify(report, manifest, trusted, check):
    check("schemas", manifest.get("schema") == MANIFEST_SCHEMA and report.get("schema") == REPORT_SCHEMA)
    body = {k: v for k, v in manifest.items() if k != "manifest_digest"}
    check("manifest_digest_recomputed", _dg(body) == manifest.get("manifest_digest"))
    if trusted is not None:
        check("manifest_digest_matches_trusted_anchor", manifest.get("manifest_digest") == trusted,
              "manifest differs from the trusted campaign (a case may have been removed or changed)")
    check("report_binds_manifest", report.get("manifest_digest") == manifest.get("manifest_digest"))
    for c in manifest["cases"]:
        check_ok = _dg({k: v for k, v in c.items() if k != "digest"}) == c.get("digest")
        if not check_ok:
            check(f"manifest_case_digest:{c.get('id')}", False)
    mcases = {c["id"]: c for c in manifest["cases"]}
    ids = [c["id"] for c in report["cases"]]
    check("case_ids_unique", len(ids) == len(set(ids)), "duplicate case ids in report")
    check("exact_case_set", set(ids) == set(mcases), f"omitted={sorted(set(mcases) - set(ids))[:3]} invented={sorted(set(ids) - set(mcases))[:3]}")
    bad_cases = []
    for c in report["cases"]:
        m = mcases.get(c["id"])
        if m is None:
            continue
        problems = []
        if c.get("digest") != m["digest"]:
            problems.append("case digest differs from manifest")
        if c.get("expected") != m["expected"]:
            problems.append("expected outcome differs from manifest")
        if c["status"] not in STATUSES:
            problems.append("unknown status")
        elif c["status"] == "SKIPPED_PRECONDITION":
            if m["precondition_holds"]:
                problems.append("skipped although the manifest says the precondition holds")
        else:
            if not m["precondition_holds"]:
                problems.append("executed although the manifest says the precondition fails")
            elif c["status"] in ("PASSED", "FAILED"):
                comp = c.get("comparison") or {}
                if m["kind"] == "mutation":
                    t = comp["target"]
                    exp = [m["expected"]["verdict"], m["expected"]["reason"]]
                    t_ok = t["expected"] == exp and t["actual"] == t["expected"]
                    if t["ok"] != t_ok:
                        problems.append("target ok flag inconsistent with expected/actual")
                    p_ok = all(p["ok"] == (p["actual"][0] == m["protected_base_verdict"]) for p in comp["protected"])
                    x_ok = all(x["valid"] == (x["actual"][0] in VERDICTS) for x in comp["collateral"])
                    if not (p_ok and x_ok):
                        problems.append("protected/collateral flags inconsistent")
                    derived = "PASSED" if (t_ok and all(p["actual"][0] == m["protected_base_verdict"] for p in comp["protected"])
                                           and all(x["actual"][0] in VERDICTS for x in comp["collateral"])) else "FAILED"
                else:
                    derived = "PASSED" if comp["relation"]["ok"] is True else "FAILED"
                if c["status"] != derived:
                    problems.append(f"status {c['status']} but the recorded comparison gives {derived}")
            elif c["status"] == "ERROR" and not c.get("error"):
                problems.append("ERROR without a diagnostic")
        if problems:
            bad_cases.append((c["id"], problems))
    check("case_comparisons_consistent", not bad_cases, "; ".join(f"{i}: {p}" for i, p in bad_cases[:3]))
    recomputed = _summarize(report["cases"])
    check("summary_recomputed_from_cases", recomputed == report.get("summary"), "totals differ from the cases listed")
    surv = sorted(c["id"] for c in report["cases"] if c["kind"] == "mutation" and c["status"] == "FAILED")
    check("survivors_listed", surv == report.get("survivors"), "surviving mutations were hidden or invented")
    body = {k: v for k, v in report.items() if k != "report_digest"}
    check("report_digest", _dg(body) == report.get("report_digest"))


def main(argv: list[str]) -> int:
    if len(argv) not in (3, 4):
        print("usage: python -m verifier_lab.campaign_checker report.json manifest.json [trusted-manifest-digest]", file=sys.stderr)
        return 64
    report = json.load(open(argv[1]))
    manifest = json.load(open(argv[2]))
    out = verify_campaign_report(report, manifest, argv[3] if len(argv) == 4 else None)
    print(json.dumps(out, indent=2))
    return 0 if out["status"] == "ACCEPTED" else 3


if __name__ == "__main__":
    sys.exit(main(sys.argv))

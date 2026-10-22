"""Counterexample package: reduced case + signature + scope; verified by reproduction in a fresh process."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from ..canonical import canonical_bytes, sha256_digest, strict_loads
from ..minimize import FailureSignature, OracleOutcome, ReductionResult, Reducer
from ..subjects.reference_store import SEMANTICS_VERSION
from .checkpoint import ExperimentCase, field_class, run_experiment

PACKAGE_SCHEMA = "verifier-lab/counterexample/1"
RULE_ID = "experiment.checkpoint"


class CheckpointOracle:
    """Failure oracle for ExperimentCase. Identity pins the reference semantics and runner version."""
    identity = f"checkpoint-experiment/1+{SEMANTICS_VERSION}"

    def __init__(self, signature: FailureSignature | None = None, factory=None):
        self._sig = signature
        self.factory = factory                  # None: the built-in candidate; else an ExternalFactory
        if factory is not None:
            self.identity = f"{CheckpointOracle.identity}+subject:{factory.identity}"   # a cached result from another program is invalid

    def bind(self, signature: FailureSignature) -> None:
        self._sig = signature

    def signature_of(self, case: ExperimentCase) -> FailureSignature | None:
        try:
            res = run_experiment(case, self.factory)
        except Exception:
            return None
        if res.errors or not res.divergences:
            return None
        d = res.divergences[0]
        reason = {"accepted": "acceptance_divergence", "observations": "observation_divergence"}.get(d.field, "state_divergence")
        return FailureSignature(RULE_ID, SEMANTICS_VERSION, "state_equivalence", reason,
                                f"{d.path}:{field_class(d.field)}")

    def evaluate(self, case: ExperimentCase) -> OracleOutcome:
        res = run_experiment(case, self.factory)
        if res.errors:
            if any(e["code"].startswith("subject_") for e in res.errors):
                raise RuntimeError(res.errors[0]["code"])       # operational: the minimizer never caches or accepts it
            return OracleOutcome.INVALID_CANDIDATE
        if not res.divergences:
            return OracleOutcome.NO_FAILURE
        sig = self.signature_of(case)
        return OracleOutcome.SAME_FAILURE if sig == self._sig else OracleOutcome.DIFFERENT_FAILURE


class LaxOracle(CheckpointOracle):
    """Deliberately wrong (positive control for the conformance suite): any divergence counts as 'the' failure."""
    identity = "lax-oracle/1"

    def evaluate(self, case):
        res = run_experiment(case)
        if res.errors:
            return OracleOutcome.INVALID_CANDIDATE
        return OracleOutcome.NO_FAILURE if not res.divergences else OracleOutcome.SAME_FAILURE


def build_package(result: ReductionResult, reducers: list[Reducer], factory=None) -> dict:
    final = run_experiment(result.best, factory)
    subject = ({"kind": "builtin"} if factory is None else
               {"kind": "external", "identity": factory.identity,
                "note": "informational only: a command is never read from a package or executed by verification"})
    body = {"schema": PACKAGE_SCHEMA, "kind": "checkpoint_experiment",
            "status": result.status, "signature": result.signature.to_dict() if result.signature else None,
            "original": {"identity": result.original.identity(), "case": result.original.to_dict()},
            "reduced": {"identity": result.best.identity(), "case": result.best.to_dict()},
            "witness": final.first().to_dict() if final.first() else None,
            "runner": {"semantics_version": SEMANTICS_VERSION, "oracle": CheckpointOracle.identity, "subject": subject},
            "reduction": {"oracle_calls": result.oracle_calls, "cache_hits": result.cache_hits,
                          "stop_reason": result.stop_reason, "exhausted_reducers": result.exhausted_reducers,
                          "scope": result.scope_statement(reducers), "log": result.log},
            "reproduction_outcomes": result.reproduction_outcomes}
    return {**body, "id": sha256_digest(canonical_bytes(body))}


def write_package(pkg: dict, path: Path) -> None:
    if path.exists():
        raise FileExistsError(f"refusing to overwrite {path}")
    path.write_bytes(canonical_bytes(pkg))


def verify_in_process(pkg: dict, factory=None) -> dict:
    """Re-evaluate the witness from the stored case alone; never trusts the reducer's assertions."""
    from .laboratory import SCHEMA as LAB_SCHEMA, verify_counterexample
    if isinstance(pkg, dict) and pkg.get("schema") == LAB_SCHEMA:
        if factory is not None:
            return {"status": "REJECTED", "checks": [{"name": "builtin_subject_required", "ok": False}]}
        return verify_counterexample(pkg)
    checks = []

    def check(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        return ok
    body = {k: v for k, v in pkg.items() if k != "id"}
    check("package_identity", sha256_digest(canonical_bytes(body)) == pkg.get("id"))
    if pkg.get("schema") != PACKAGE_SCHEMA or pkg.get("kind") != "checkpoint_experiment":
        check("schema", False, "unsupported package")
        return {"status": "REJECTED", "checks": checks}
    case = ExperimentCase.from_dict(pkg["reduced"]["case"])
    original = ExperimentCase.from_dict(pkg["original"]["case"])
    check("reduced_identity", case.identity() == pkg["reduced"]["identity"])
    check("original_identity", original.identity() == pkg["original"]["identity"])
    check("semantics_version", pkg["runner"]["semantics_version"] == SEMANTICS_VERSION,
          f"{pkg['runner']['semantics_version']} vs {SEMANTICS_VERSION}")
    sig = FailureSignature.from_dict(pkg["signature"]) if pkg.get("signature") else None
    check("signature_recorded", sig is not None)
    subject = pkg["runner"].get("subject", {"kind": "builtin"})
    if subject["kind"] == "external":
        supplied = factory is not None and factory.identity == subject["identity"]
        check("subject_supplied_by_caller", supplied,
              "this package names an external subject; verification runs only a program the caller supplies, "
              "and its identity must equal the recorded one" if not supplied else "")
        if not supplied:
            return {"status": "REJECTED", "checks": checks}
    elif factory is not None:
        check("subject_matches_package", False, "package was produced by the built-in subject")
        return {"status": "REJECTED", "checks": checks}
    if sig:
        res = run_experiment(case, factory)
        orc = CheckpointOracle(factory=factory)
        actual = orc.signature_of(case)
        check("reduced_case_fails_with_same_signature", actual == sig, f"actual={actual and actual.to_dict()}")
        check("witness_reproduces", bool(res.first()) and res.first().to_dict() == pkg.get("witness"))
        orig_sig = orc.signature_of(original)
        check("original_has_same_signature", orig_sig == sig)
        check("not_larger_than_original", case.cost() <= original.cost())
    return {"status": "VERIFIED" if all(c["ok"] for c in checks) else "REJECTED", "checks": checks}


_BOOT = ("import sys, json; sys.path.insert(0, sys.argv[1]);"
         "from verifier_lab.experiments.package import verify_in_process;"
         "from verifier_lab.canonical import strict_loads;"
         "from verifier_lab.subjects.external import ExternalFactory;"
         "cmd = json.loads(sys.argv[3]) if len(sys.argv) > 3 else None; env = json.loads(sys.argv[4]) if len(sys.argv) > 4 else {};"
         "out = verify_in_process(strict_loads(open(sys.argv[2], 'rb').read(), max_bytes=64_000_000), ExternalFactory(cmd, env=env) if cmd else None);"
         "print(json.dumps(out))")


def verify_package_fresh_process(path: Path, timeout: float = 120.0, command: list[str] | None = None,
                                 env: dict | None = None) -> dict:
    """Run verification in a new interpreter that shares no memory with the reducer."""
    root = str(Path(__file__).resolve().parents[2])
    with tempfile.TemporaryDirectory(prefix="vl-verify-") as cwd:
        argv = [sys.executable, "-E", "-s", "-c", _BOOT, root, str(Path(path).resolve())] + ([json.dumps(command), json.dumps(env or {})] if command else [])
        proc = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout,
                              env={"PATH": os.environ.get("PATH", "") if command else ""})
    if proc.returncode != 0:
        return {"status": "REJECTED", "checks": [{"name": "verifier_process", "ok": False, "detail": proc.stderr[-300:]}]}
    return json.loads(proc.stdout)

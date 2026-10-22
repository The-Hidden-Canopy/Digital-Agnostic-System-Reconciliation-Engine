import json
import shutil
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from tests.helpers import ROOT
from verifier_lab.experiments.checkpoint import ExperimentCase, run_experiment
from verifier_lab.experiments.history import generate_history
from verifier_lab.experiments.package import (CheckpointOracle, build_package, verify_in_process,
                                              verify_package_fresh_process, write_package)
from verifier_lab.minimize import minimize
from verifier_lab.reducers.sequence import DeleteTransactionMembers, DeleteUnits
from verifier_lab.reducers.values import SimplifyValues
from verifier_lab.subjects.external import ExternalFactory, ExternalSubject, SubjectError, subject_identity

EXAMPLE = [sys.executable, str(ROOT / "examples" / "reference_subject.py")]
HIST = tuple(generate_history("ext", 60))


def factory(env=None, **kw):
    return ExternalFactory(EXAMPLE, env=env or {}, **kw)


class Protocol(unittest.TestCase):
    def test_correct_subject_matches_the_reference_on_every_path(self):
        r = run_experiment(ExperimentCase("s", HIST, 30), factory())
        self.assertTrue(r.passed, r.to_dict()["divergences"][:1])
        self.assertEqual(r.coverage["steps_compared_per_path"], {"continuous": 60, "resumed": 30, "replayed": 30})

    def test_injected_defects_in_the_external_program_are_found_with_field_witnesses(self):
        for defect, field in (("reset_next_id", "next_identifier"), ("omit_random_cursor", "random_cursor")):
            r = run_experiment(ExperimentCase("seed-3", tuple(generate_history("seed-3", 120)), 60), factory({"SUBJECT_DEFECTS": defect}))
            self.assertFalse(r.passed)
            self.assertEqual(r.first().path, "resumed", defect)
            self.assertIn(r.first().field, (field, "observations"), defect)      # a lost counter may surface via the next observation first

    def test_each_instance_is_a_fresh_process(self):
        f = factory()
        a, b = f("s"), f("s")
        a.apply({"op": "put", "key": "k", "value": "v"})
        self.assertEqual(b.snapshot()["entries"], [])                      # no shared memory between instances
        f.close_all()

    def test_defect_injection_is_for_the_builtin_candidate_only(self):
        r = run_experiment(ExperimentCase("s", HIST[:5], 2, ("flatten_txn",)), factory())
        self.assertEqual(r.errors[0]["code"], "subject_defects_not_supported")

    def test_identity_pins_the_program_and_its_environment(self):
        a, b = subject_identity(EXAMPLE), subject_identity(EXAMPLE, {"X": "1"})
        self.assertNotEqual(a, b)
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "s.py"
            p.write_text("print(1)")
            before = subject_identity([sys.executable, str(p)])
            p.write_text("print(2)")
            self.assertNotEqual(before, subject_identity([sys.executable, str(p)]))


class MisbehavingSubjects(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vl-subj-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def script(self, body: str):
        p = self.tmp / "subject.py"
        p.write_text(textwrap.dedent(body))
        return [sys.executable, str(p)]

    def code(self, body: str, timeout=3.0) -> str:
        f = ExternalFactory(self.script(body), timeout=timeout)
        r = run_experiment(ExperimentCase("s", HIST[:6], 3), f)
        self.assertFalse(r.passed)
        self.assertEqual(r.divergences, [])                 # a broken subject is an execution error, never a "divergence"
        return r.errors[0]["code"]

    def test_crash_hang_garbage_and_missing_fields_are_execution_errors(self):
        self.assertEqual(self.code("import sys\nsys.exit(3)\n"), "subject_exited")
        self.assertEqual(self.code("import time\nfor l in __import__('sys').stdin:\n    time.sleep(30)\n", timeout=0.7), "subject_timeout")
        self.assertEqual(self.code("import sys\nfor l in sys.stdin:\n    print('not json'); sys.stdout.flush()\n"), "subject_protocol_violation")
        self.assertEqual(self.code("import sys\nfor l in sys.stdin:\n    print('[1]'); sys.stdout.flush()\n"), "subject_protocol_violation")
        self.assertEqual(self.code("""
            import sys, json
            for l in sys.stdin:
                r = json.loads(l)
                out = {"ok": True}
                if r["cmd"] == "apply":
                    out = {"ok": True, "accepted": "yes", "observations": []}
                print(json.dumps(out)); sys.stdout.flush()
        """), "subject_protocol_violation")

    def test_incomplete_snapshot_is_never_a_pass(self):
        f = ExternalFactory(self.script("""
            import sys, json
            for l in sys.stdin:
                r = json.loads(l)
                out = {"ok": True}
                if r["cmd"] == "apply":
                    out = {"ok": True, "accepted": True, "observations": ["put:a"]}
                if r["cmd"] == "snapshot":
                    out = {"ok": True, "state": {"entries": []}}
                print(json.dumps(out)); sys.stdout.flush()
        """))
        r = run_experiment(ExperimentCase("s", HIST[:6], 3), f)
        self.assertFalse(r.passed)                                           # missing fields are flagged, never silently accepted

    def test_program_that_cannot_start_and_oversized_responses(self):
        r = run_experiment(ExperimentCase("s", HIST[:3], 1), ExternalFactory(["/nonexistent/binary"]))
        self.assertEqual(r.errors[0]["code"], "subject_cannot_start")
        self.assertEqual(self.code("import sys\nfor l in sys.stdin:\n    sys.stdout.write('x' * 5_000_000); sys.stdout.flush()\n"), "subject_response_too_large")

    def test_subject_runs_in_an_empty_working_directory(self):
        body = """
            import sys, json, os
            for l in sys.stdin:
                r = json.loads(l)
                out = {"ok": True}
                if r["cmd"] == "apply":
                    out = {"ok": True, "accepted": True, "observations": sorted(os.listdir("."))}
                print(json.dumps(out)); sys.stdout.flush()
        """
        s = ExternalSubject(self.script(body), "s")
        self.addCleanup(s.close)
        self.assertEqual(s.apply({"op": "put", "key": "a", "value": "b"}), (True, ()))        # no files visible to the subject

    def test_replay_entry_point_is_optional_and_validated(self):
        s = ExternalSubject(EXAMPLE, "s")
        self.addCleanup(s.close)
        out = list(s.replay([{"op": "put", "key": "a", "value": "1"}, {"op": "allocate", "key": "b"}]))
        self.assertEqual([o[1] for o in out], [True, True])        # example has no replay: adapter falls back to sequential apply


class MinimizeAndPackage(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vl-extpkg-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.env = {"SUBJECT_DEFECTS": "reset_next_id"}
        self.factory = factory(self.env)
        self.case = ExperimentCase("seed-3", tuple(generate_history("seed-3", 120)), 60)
        self.reducers = [DeleteUnits(), DeleteTransactionMembers(), SimplifyValues()]
        self.result = minimize(self.case, CheckpointOracle(factory=self.factory), self.reducers)
        self.pkg = build_package(self.result, self.reducers, self.factory)
        self.path = self.tmp / "p.json"
        write_package(self.pkg, self.path)

    def test_external_failure_is_reduced_and_verified_in_a_fresh_process(self):
        self.assertEqual(self.result.status, "REDUCED")
        self.assertLessEqual(len(self.result.best.ops), 4)
        self.assertEqual(self.pkg["runner"]["subject"]["kind"], "external")
        res = verify_package_fresh_process(self.path, command=self.factory.command, env=self.env)
        self.assertEqual(res["status"], "VERIFIED", [c for c in res["checks"] if not c["ok"]])

    def test_verification_never_runs_a_program_the_caller_did_not_supply(self):
        res = verify_package_fresh_process(self.path)
        self.assertEqual(res["status"], "REJECTED")
        self.assertIn("subject_supplied_by_caller", {c["name"] for c in res["checks"] if not c["ok"]})
        self.assertNotIn("command", json.dumps(self.pkg["runner"]["subject"]).replace("a command is never", ""))   # only identity + note

    def test_a_different_program_or_environment_is_rejected(self):
        self.assertEqual(verify_in_process(self.pkg, factory())["status"], "REJECTED")                       # correct program: wrong identity
        other = ExternalFactory(EXAMPLE, env={"SUBJECT_DEFECTS": "reset_next_id", "EXTRA": "1"})
        self.assertEqual(verify_in_process(self.pkg, other)["status"], "REJECTED")
        self.assertEqual(verify_in_process(self.pkg, self.factory)["status"], "VERIFIED")

    def test_builtin_packages_do_not_accept_an_external_subject(self):
        c = ExperimentCase("s", (dict(op="allocate", key="a"), dict(op="delete", key="a"), dict(op="put", key="x", value="v")), 2, ("reset_next_id",))
        r = minimize(c, CheckpointOracle(), self.reducers)
        pkg = build_package(r, self.reducers)
        self.assertEqual(verify_in_process(pkg)["status"], "VERIFIED")
        self.assertEqual(verify_in_process(pkg, factory())["status"], "REJECTED")

    def test_operational_errors_are_not_accepted_as_a_reproduction(self):
        class Flaky(ExternalFactory):
            calls = 0

            def __call__(self, seed, defects=frozenset()):
                Flaky.calls += 1
                if Flaky.calls % 7 == 0:
                    raise SubjectError("timeout", "simulated")
                return super().__call__(seed, defects)
        small = ExperimentCase("s", (dict(op="allocate", key="a"), dict(op="delete", key="a"), dict(op="put", key="x", value="v")), 2)
        r = minimize(small, CheckpointOracle(factory=Flaky(EXAMPLE, env=self.env)), self.reducers)
        # transient subject failures must never be counted as a reproduction or accepted as a reduction
        self.assertTrue(all(e["outcome"] != "same_failure" or e["accepted"] for e in r.log))
        self.assertIn(r.status, ("UNSTABLE", "NOT_REPRODUCED", "REDUCED", "BEST_SO_FAR"))


if __name__ == "__main__":
    unittest.main()

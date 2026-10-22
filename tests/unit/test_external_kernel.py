import shutil
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from tests.helpers import ROOT
from verifier_lab import campaign_checker as CC
from verifier_lab.harness import campaign as C
from verifier_lab.harness.external_kernel import ExternalKernel
from verifier_lab.harness.world import RealKernel, base_scenarios
from verifier_lab.subjects.external import SubjectError

EXAMPLE = [sys.executable, str(ROOT / "examples" / "reference_verifier.py")]
MANIFEST = C.build_manifest()


def run(kernel):
    return C.run_campaign(kernel, MANIFEST)


_CACHE = {}


def example_report(env=None):
    """Campaigns spawn a process per case, so identical runs are computed once."""
    key = tuple(sorted((env or {}).items()))
    if key not in _CACHE:
        _CACHE[key] = run(ExternalKernel(EXAMPLE, env=env))
    return _CACHE[key]


class ExampleVerifier(unittest.TestCase):
    def test_a_partial_verifier_is_judged_only_by_its_answers(self):
        r = example_report()
        self.assertTrue(r["kernel"]["name"].startswith("external:"))
        c = r["summary"]["counts"]
        self.assertEqual(c["ERROR"], 0)
        self.assertGreater(c["PASSED"], 0)
        self.assertGreater(len(r["survivors"]), 0)                   # it does not implement interval.relation at all
        failed_preds = {x["predicate"] for x in r["cases"] if x["kind"] == "mutation" and x["status"] == "FAILED"}
        self.assertIn("interval.relation", failed_preds)
        ok_ops = {x["operator"] for x in r["cases"] if x["kind"] == "mutation" and x["status"] == "PASSED" and x["predicate"] == "fact.equals"}
        self.assertTrue({"change_value", "remove_required_evidence", "conflicting_second_source", "change_value_kind"} <= ok_ops)

    def test_report_is_independently_verifiable_and_anchored(self):
        r = example_report()
        res = CC.verify_campaign_report(r, MANIFEST, MANIFEST["manifest_digest"])
        self.assertEqual(res["status"], "ACCEPTED", [c for c in res["checks"] if not c["ok"]])

    def test_injected_bug_in_the_external_program_is_caught_where_it_lives(self):
        actual = lambda rep: {x["id"]: (x["comparison"]["target"]["actual"] if x["comparison"] and "target" in x["comparison"] else None)
                              for x in rep["cases"] if x["kind"] == "mutation"}
        clean, buggy = actual(example_report()), actual(example_report({"VERIFIER_DEFECTS": "last_key_wins"}))
        changed = {k for k in clean if clean[k] != buggy[k]}
        self.assertTrue(changed)
        self.assertTrue(all("duplicate_key" in k for k in changed), changed)          # the bug surfaces only where it lives
        self.assertTrue(any(buggy[k][0] == "SUPPORTED" for k in changed))             # silently resolved instead of flagged

    def test_identity_pins_program_and_environment(self):
        self.assertNotEqual(ExternalKernel(EXAMPLE).identity, ExternalKernel(EXAMPLE, env={"X": "1"}).identity)
        self.assertEqual(ExternalKernel(EXAMPLE).name, ExternalKernel(EXAMPLE).name)

    def test_scenario_is_delivered_with_digests_so_the_program_can_check_its_input(self):
        s = base_scenarios()[0]
        out = ExternalKernel(EXAMPLE).verify(s)
        self.assertEqual(set(out), {c.key for c in s.claims})
        self.assertEqual(out["c_eq"], ("SUPPORTED", "value_equal"))
        self.assertIn("sha256", s.to_dict()["evidence"][0])


class MisbehavingVerifiers(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vl-kern-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def kernel(self, body: str, timeout=3.0):
        p = self.tmp / "v.py"
        p.write_text(textwrap.dedent(body))
        return ExternalKernel([sys.executable, str(p)], timeout=timeout)

    def check_errors(self, body, timeout=3.0):
        r = run(self.kernel(body, timeout))
        c = r["summary"]["counts"]
        self.assertEqual(c["PASSED"], 0, "a broken verifier must never score a pass")
        self.assertGreater(c["ERROR"], 0)
        self.assertTrue(all(x["error"] for x in r["cases"] if x["status"] == "ERROR"))
        self.assertFalse(all(r["base_expectations_reproduced"].values()))
        return r

    def test_crash_hang_garbage_and_malformed_outcomes_are_errors_not_passes(self):
        self.check_errors("import sys\nsys.exit(2)\n")
        hang = self.kernel("import sys, time\nfor l in sys.stdin:\n    time.sleep(30)\n", timeout=0.6)
        with self.assertRaises(SubjectError) as cm:
            hang.verify(base_scenarios()[0])
        self.assertEqual(cm.exception.code, "timeout")
        case = next(c for c in MANIFEST["cases"] if c["precondition_holds"])      # in a campaign a hang is one ERROR case, never a pass
        out = C.run_case(case, hang, {s.id: s for s in base_scenarios()})
        self.assertEqual(out["status"], "ERROR")
        self.assertIn("timeout", out["error"])
        self.check_errors("import sys\nfor l in sys.stdin:\n    print('not json', flush=True)\n")
        self.check_errors("import sys\nfor l in sys.stdin:\n    print('{\"ok\": true, \"outcomes\": {\"c\": [\"MAYBE\", \"x\"]}}', flush=True)\n")
        self.check_errors("import sys\nfor l in sys.stdin:\n    print('{\"ok\": true}', flush=True)\n")

    def test_an_always_supported_external_program_fails_the_campaign(self):
        r = run(self.kernel("""
            import sys, json
            for l in sys.stdin:
                s = json.loads(l)["scenario"]
                print(json.dumps({"ok": True, "outcomes": {c["key"]: ["SUPPORTED", "value_equal"] for c in s["claims"]}}), flush=True)
        """))
        self.assertGreater(len(r["survivors"]), 40)
        self.assertEqual(r["summary"]["mutation_detection"]["detected"], 0)

    def test_a_program_that_cannot_start_is_an_error_everywhere(self):
        r = run(ExternalKernel(["/nonexistent/verifier"]))
        self.assertEqual(r["summary"]["counts"]["PASSED"], 0)

    def test_real_kernel_still_passes_untouched(self):
        self.assertEqual(run(RealKernel())["survivors"], [])


if __name__ == "__main__":
    unittest.main()

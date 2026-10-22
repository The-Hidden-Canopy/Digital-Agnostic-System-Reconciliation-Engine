import copy
import json
import unittest

from verifier_lab import campaign_checker as CC
from verifier_lab.canonical import canonical_bytes, sha256_digest
from verifier_lab.harness import campaign as C
from verifier_lab.harness.metamorphic import RELATIONS
from verifier_lab.harness.mutation import OPERATORS, mutate
from verifier_lab.harness.world import (KERNELS, AlwaysSupported, ClaimSpec, Doc, RealKernel, Scenario, base_scenarios)


class Bases(unittest.TestCase):
    def test_every_base_claim_is_supported_by_the_real_kernel_as_specified_by_hand(self):
        for s in base_scenarios():
            out = RealKernel().verify(s)
            self.assertEqual(set(out), {c.key for c in s.claims})
            self.assertTrue(all(v[0] == "SUPPORTED" for v in out.values()), (s.id, out))

    def test_regression_corrupt_interval_evidence_is_invalid_not_insufficient(self):
        """Found by the first campaign: interval.relation reported INSUFFICIENT for unreadable evidence."""
        s = base_scenarios()[1]
        broken = Scenario(s.id, s.origin, (Doc("log.json", "json", b'{"a": {"start": '),), s.claims)
        self.assertEqual({v[0] for v in RealKernel().verify(broken).values()}, {"INVALID_EVIDENCE"})


class Mutations(unittest.TestCase):
    def test_every_operator_runs_against_a_compatible_base_and_matches_its_committed_expectation(self):
        ran = {}
        for s in base_scenarios():
            for c in s.claims:
                for op in OPERATORS:
                    if not op.applies(s, c):
                        continue
                    m = mutate(s, c.key, op.id, "t")
                    out = RealKernel().verify(m.scenario)
                    self.assertEqual(tuple(out[c.key]), op.expected(c), (op.id, s.id, c.key))
                    ran[op.id] = ran.get(op.id, 0) + 1
        self.assertEqual(set(ran), {o.id for o in OPERATORS})            # no operator is left unexercised

    def test_mutation_is_non_destructive_deterministic_and_recorded(self):
        s = base_scenarios()[0]
        before = s.digest()
        a, b = mutate(s, "c_eq", "change_value", "seed"), mutate(s, "c_eq", "change_value", "seed")
        self.assertEqual((s.digest(), a.record.base_digest), (before, before))
        self.assertNotEqual(a.record.result_digest, before)
        self.assertEqual(a.record.to_dict(), b.record.to_dict())
        self.assertNotEqual(mutate(s, "c_eq", "change_value", "other-seed").record.result_digest, "")

    def test_layers_are_labelled_so_hash_failures_are_not_counted_as_semantic_coverage(self):
        layers = {o.id: o.layer for o in OPERATORS}
        self.assertEqual({k for k, v in layers.items() if v == "byte"}, {"truncate_evidence_bytes", "invalid_utf8_evidence"})
        self.assertEqual(sum(1 for v in layers.values() if v == "semantic"), 8)

    def test_precondition_failure_is_skipped_not_passed(self):
        m = C.build_manifest()
        r = C.run_campaign("real", m)
        skipped = [c for c in r["cases"] if c["status"] == "SKIPPED_PRECONDITION"]
        self.assertTrue(skipped)
        self.assertTrue(all(c["comparison"] is None for c in skipped))
        s = r["summary"]
        self.assertEqual(s["counts"]["PASSED"] + s["counts"]["FAILED"] + s["counts"]["SKIPPED_PRECONDITION"] + s["counts"]["ERROR"], s["total_cases"])
        self.assertEqual(s["mutation_detection"]["executed_mutations"],
                         sum(1 for c in r["cases"] if c["kind"] == "mutation" and c["status"] in ("PASSED", "FAILED")))


class Metamorphic(unittest.TestCase):
    def test_each_relation_has_a_satisfying_example_and_a_precondition_failing_example(self):
        sat = {r.relation_id: [s.id for s in base_scenarios() if r.precondition(s)] for r in RELATIONS}
        self.assertTrue(all(sat.values()), sat)
        no_json = Scenario("S-md", Doc("o.md", "markdown", b"---\nk: 1\n---\n"), (Doc("e.md", "markdown", b"---\nk: 1\n---\n"),),
                           (ClaimSpec("c", "fact.equals", "k", {"expected": {"kind": "integer", "value": "1"}}),))
        failing = {r.relation_id for r in RELATIONS if not r.precondition(no_json)}
        self.assertEqual(failing, {"reorder_json_keys", "rename_opaque_identifiers", "add_irrelevant_artifact", "remove_only_witness", "increase_uncertainty"})
        self.assertIn("reorder_claims_and_evidence", {r.relation_id for r in RELATIONS if r.precondition(no_json)})

    def test_relations_hold_for_the_real_kernel_and_catch_broken_ones(self):
        r = C.run_campaign("real")
        meta = [c for c in r["cases"] if c["layer"] == "metamorphic" and c["status"] != "SKIPPED_PRECONDITION"]
        self.assertTrue(meta and all(c["status"] == "PASSED" for c in meta), [c["id"] for c in meta if c["status"] != "PASSED"])
        bad = C.run_campaign("always_supported")
        self.assertTrue(any(c["layer"] == "metamorphic" and c["status"] == "FAILED" for c in bad["cases"]))   # remove_only_witness trips


class Campaign(unittest.TestCase):
    def test_manifest_is_deterministic_and_pins_every_case(self):
        a, b = C.build_manifest("s"), C.build_manifest("s")
        self.assertEqual(a, b)
        self.assertNotEqual(a["manifest_digest"], C.build_manifest("t")["manifest_digest"])
        self.assertEqual(len({c["id"] for c in a["cases"]}), len(a["cases"]))
        self.assertTrue(all(c["digest"] == C._digest({k: v for k, v in c.items() if k != "digest"}) for c in a["cases"]))

    def test_real_kernel_passes_everything_applicable(self):
        r = C.run_campaign("real")
        self.assertEqual((r["summary"]["counts"]["FAILED"], r["summary"]["counts"]["ERROR"], r["survivors"]), (0, 0, []))
        self.assertEqual(r["summary"]["never_executed"], [])
        self.assertTrue(all(r["base_expectations_reproduced"].values()))
        self.assertEqual(r["summary"]["mutation_detection"]["detected"], r["summary"]["mutation_detection"]["executed_mutations"])

    def test_every_intentionally_defective_kernel_fails_the_campaign(self):
        """An all-green campaign against a defective verifier would be a harness failure."""
        for name in ("always_supported", "always_contradicted", "last_key_wins", "self_reference_allowed"):
            r = C.run_campaign(name)
            self.assertGreater(len(r["survivors"]), 0, name)
            self.assertGreater(r["summary"]["counts"]["FAILED"], 0, name)
        self.assertEqual(C.run_campaign("always_supported")["summary"]["mutation_detection"]["detected"], 0)

    def test_subtle_defects_are_attributed_to_the_right_operators(self):
        surv = lambda k: {c["operator"] for c in C.run_campaign(k)["cases"] if c["kind"] == "mutation" and c["status"] == "FAILED"}
        self.assertEqual(surv("self_reference_allowed"), {"assertion_as_its_own_evidence"})
        self.assertIn("duplicate_key_in_evidence", surv("last_key_wins"))

    def test_kernel_exceptions_are_errors_not_passes(self):
        class Boom:
            name = "boom"

            def verify(self, s):
                raise RuntimeError("kernel crashed")
        r = C.run_campaign(Boom())
        self.assertGreater(r["summary"]["counts"]["ERROR"], 0)
        self.assertEqual(r["summary"]["counts"]["PASSED"], 0)
        self.assertTrue(all(c["error"] for c in r["cases"] if c["status"] == "ERROR"))
        self.assertFalse(all(r["base_expectations_reproduced"].values()))

    def test_report_is_deterministic(self):
        self.assertEqual(C.run_campaign("real")["report_digest"], C.run_campaign("real")["report_digest"])


class IndependentChecker(unittest.TestCase):
    def setUp(self):
        self.manifest = C.build_manifest()
        self.report = C.run_campaign("real", self.manifest)
        self.anchor = self.manifest["manifest_digest"]

    def verify(self, report=None, manifest=None, anchor="default"):
        return CC.verify_campaign_report(self.report if report is None else report, manifest or self.manifest, self.anchor if anchor == "default" else anchor)

    def failed(self, res):
        return {c["name"] for c in res["checks"] if not c["ok"]}

    def reseal(self, report):
        body = {k: v for k, v in report.items() if k != "report_digest"}
        return {**body, "report_digest": CC._dg(body)}

    def test_honest_reports_are_accepted_including_ones_that_document_failures(self):
        self.assertEqual(self.verify()["status"], "ACCEPTED")
        bad = C.run_campaign("always_supported", self.manifest)
        res = self.verify(bad)
        self.assertEqual(res["status"], "ACCEPTED")                      # internally valid even though it documents 46 failures
        self.assertGreater(len(bad["survivors"]), 0)

    def test_forged_passing_totals_and_flipped_statuses_are_rejected(self):
        bad = C.run_campaign("always_supported", self.manifest)
        forged = copy.deepcopy(bad)
        forged["summary"]["counts"]["PASSED"] += forged["summary"]["counts"]["FAILED"]
        forged["summary"]["counts"]["FAILED"] = 0
        self.assertIn("summary_recomputed_from_cases", self.failed(self.verify(self.reseal(forged))))
        flipped = copy.deepcopy(bad)
        for c in flipped["cases"]:
            if c["status"] == "FAILED":
                c["status"] = "PASSED"
        flipped["survivors"] = []
        flipped["summary"] = CC._summarize(flipped["cases"])
        self.assertIn("case_comparisons_consistent", self.failed(self.verify(self.reseal(flipped))))

    def test_hidden_survivors_and_altered_expectations_are_rejected(self):
        bad = C.run_campaign("always_supported", self.manifest)
        hidden = copy.deepcopy(bad)
        hidden["survivors"] = hidden["survivors"][1:]
        self.assertIn("survivors_listed", self.failed(self.verify(self.reseal(hidden))))
        altered = copy.deepcopy(bad)
        for c in altered["cases"]:
            if c["status"] == "FAILED" and c["kind"] == "mutation":
                c["expected"] = {"verdict": "SUPPORTED", "reason": "value_equal"}
                c["comparison"]["target"]["expected"] = ["SUPPORTED", "value_equal"]
                c["comparison"]["target"]["ok"] = c["comparison"]["target"]["actual"] == ["SUPPORTED", "value_equal"]
                break
        self.assertIn("case_comparisons_consistent", self.failed(self.verify(self.reseal(altered))))

    def test_omitted_duplicate_and_invented_cases(self):
        r = copy.deepcopy(self.report)
        r["cases"] = r["cases"][1:]
        self.assertIn("exact_case_set", self.failed(self.verify(self.reseal(r))))
        r = copy.deepcopy(self.report)
        r["cases"].append(copy.deepcopy(r["cases"][0]))
        self.assertIn("case_ids_unique", self.failed(self.verify(self.reseal(r))))
        r = copy.deepcopy(self.report)
        r["cases"][0]["id"] = "mut:invented:S-x:c"
        self.assertIn("exact_case_set", self.failed(self.verify(self.reseal(r))))

    def test_skip_laundering_is_rejected(self):
        bad = C.run_campaign("always_supported", self.manifest)
        r = copy.deepcopy(bad)
        victim = next(c for c in r["cases"] if c["status"] == "FAILED" and c["kind"] == "mutation")
        victim.update(status="SKIPPED_PRECONDITION", comparison=None)
        r["survivors"] = [x for x in r["survivors"] if x != victim["id"]]
        r["summary"] = CC._summarize(r["cases"])
        self.assertIn("case_comparisons_consistent", self.failed(self.verify(self.reseal(r))))

    def test_self_consistent_report_for_a_weakened_manifest_needs_the_trusted_anchor(self):
        """Attacker deletes a failing case and recomputes manifest AND report consistently."""
        bad = C.run_campaign("always_supported", self.manifest)
        drop = bad["survivors"][0]
        m2 = copy.deepcopy(self.manifest)
        m2["cases"] = [c for c in m2["cases"] if c["id"] != drop]
        m2["manifest_digest"] = CC._dg({k: v for k, v in m2.items() if k != "manifest_digest"})
        r2 = copy.deepcopy(bad)
        r2["cases"] = [c for c in r2["cases"] if c["id"] != drop]
        r2["survivors"] = [s for s in r2["survivors"] if s != drop]
        r2["summary"] = CC._summarize(r2["cases"])
        r2["manifest_digest"] = m2["manifest_digest"]
        r2 = self.reseal(r2)
        unanchored = CC.verify_campaign_report(r2, m2, None)
        self.assertEqual(unanchored["status"], "ACCEPTED")                       # internally consistent...
        self.assertFalse(unanchored["anchored"])
        self.assertIn("does NOT show", unanchored["conclusion"])                 # ...and labelled as the narrower conclusion
        anchored = CC.verify_campaign_report(r2, m2, self.anchor)
        self.assertEqual(anchored["status"], "REJECTED")
        self.assertIn("manifest_digest_matches_trusted_anchor", self.failed(anchored))

    def test_malformed_reports_are_rejected_not_crashes(self):
        self.assertEqual(self.verify({"schema": "x", "cases": [{"id": "a"}]})["status"], "REJECTED")
        self.assertEqual(self.verify({})["status"], "REJECTED")

    def test_checker_imports_nothing_from_the_producer(self):
        import ast
        from pathlib import Path
        src = (Path(__file__).resolve().parents[2] / "src/verifier_lab/campaign_checker.py").read_text()
        for node in ast.walk(ast.parse(src)):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""] if isinstance(node, ast.ImportFrom) else []
            self.assertFalse(any(n.startswith("verifier_lab") for n in names) or (isinstance(node, ast.ImportFrom) and node.level), names)

    def test_a_deliberately_wrong_checker_is_caught_by_fixed_oracle_cases(self):
        """The checker is itself tested: a checker that accepts everything fails these fixed cases."""
        def lenient(report, manifest, anchor=None):
            return {"status": "ACCEPTED", "checks": []}
        forged = copy.deepcopy(C.run_campaign("always_supported", self.manifest))
        forged["summary"]["counts"]["PASSED"] = 999
        self.assertEqual(lenient(forged, self.manifest)["status"], "ACCEPTED")            # the lenient checker is fooled...
        self.assertEqual(CC.verify_campaign_report(forged, self.manifest, self.anchor)["status"], "REJECTED")   # ...the real one is not


if __name__ == "__main__":
    unittest.main()

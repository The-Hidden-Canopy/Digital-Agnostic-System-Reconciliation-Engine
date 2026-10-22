import copy
import json
import random
import unittest

from tests.helpers import ROOT
from tests.unit.test_rules_engine import Lab, one
from verifier_lab import checker
from verifier_lab import composition as CP
from verifier_lab.model import Verdict

FIX = ROOT / "fixtures" / "composition"


def load(name):
    return json.loads((FIX / f"{name}.json").read_text())["pipeline"]


def analyze(name, target=None):
    return CP.analyze(CP.parse_pipeline(load(name)), target)


def reasons(a):
    return {o["id"]: (o["verdict"], o["reason"]) for o in a["obligations"]}


class Fixtures(unittest.TestCase):
    def test_expected_outcomes_match_fixture_readme(self):
        expect = {"good": "SUPPORTED", "units-mismatch": "CONTRADICTED", "units-converted": "SUPPORTED", "units-bad-conversion": "CONTRADICTED",
                  "offsets-mismatch": "CONTRADICTED", "missing-input": "INSUFFICIENT_EVIDENCE", "cycle": "CONTRADICTED",
                  "generation-mismatch": "CONTRADICTED", "scoped": "INSUFFICIENT_EVIDENCE"}
        for name, verdict in expect.items():
            self.assertEqual(analyze(name)["overall"], verdict, name)

    def test_locally_valid_parts_fail_globally_with_a_witness_naming_both_sides(self):
        a = analyze("units-mismatch")
        o = next(x for x in a["obligations"] if x["id"] == "input:alert:latency")
        self.assertEqual((o["verdict"], o["reason"]), ("CONTRADICTED", "tag_mismatch"))
        self.assertEqual(o["witness"]["producer"], "measure")
        self.assertEqual(o["witness"]["consumer"], "alert")
        self.assertEqual(o["witness"]["mismatches"], [{"tag": "unit", "requirement": {"equals": "seconds"}, "guarantee": "milliseconds"}])
        self.assertEqual(reasons(a)["graph:acyclic"][0], "SUPPORTED")                    # ordering is fine; composition is not

    def test_no_conversion_is_inferred_from_familiar_unit_names(self):
        self.assertEqual(reasons(analyze("units-mismatch"))["input:alert:latency"][1], "tag_mismatch")
        self.assertEqual(reasons(analyze("units-converted"))["conversion:to_seconds"], ("SUPPORTED", "conversion_verified"))
        self.assertEqual(reasons(analyze("units-bad-conversion"))["conversion:to_seconds"], ("CONTRADICTED", "conversion_factor_wrong"))
        p = copy.deepcopy(load("units-converted"))
        p["activities"][1]["conversion"] = {"from": "furlongs", "to": "fortnights", "factor": "1"}
        r = reasons(CP.analyze(CP.parse_pipeline(p)))
        self.assertEqual(r["conversion:to_seconds"], ("INSUFFICIENT_EVIDENCE", "conversion_not_verifiable"))
        p["activities"][1]["conversion"] = {"from": "milliseconds", "to": "seconds", "factor": "not a number"}
        self.assertEqual(reasons(CP.analyze(CP.parse_pipeline(p)))["conversion:to_seconds"], ("INVALID_EVIDENCE", "factor_not_decimal"))
        p["activities"][1]["produces"][0]["tags"]["unit"] = "bytes"                        # conversion record disagrees with its own tags
        p["activities"][1]["conversion"] = {"from": "milliseconds", "to": "seconds", "factor": "0.001"}
        self.assertEqual(reasons(CP.analyze(CP.parse_pipeline(p)))["conversion:to_seconds"], ("CONTRADICTED", "conversion_tags_inconsistent"))

    def test_ordering_is_not_closure(self):
        a = analyze("missing-input")
        r = reasons(a)
        self.assertEqual(r["graph:acyclic"][0], "SUPPORTED")
        self.assertEqual(r["input:report:figures"], ("INSUFFICIENT_EVIDENCE", "undeclared_input"))
        self.assertEqual(a["overall"], "INSUFFICIENT_EVIDENCE")

    def test_cycle_witness_is_a_cycle_and_nothing_is_cut_silently(self):
        a = analyze("cycle")
        w = next(o for o in a["obligations"] if o["id"] == "graph:acyclic")["witness"]
        self.assertEqual(w["cycle"][0], w["cycle"][-1])
        self.assertEqual(set(w["cycle"]), {"a", "b"})
        self.assertEqual(a["order"], [])

    def test_external_inputs_need_a_declared_assumption_and_assumptions_stay_visible(self):
        a = analyze("good")
        self.assertEqual(a["overall"], "SUPPORTED")
        self.assertTrue(any("supplied by the operator" in c for c in a["conditional_on"]))
        self.assertTrue(any("producer assertions" in c for c in a["conditional_on"]))
        p = copy.deepcopy(load("good"))
        del p["external"][0]["assumption"]
        self.assertEqual(reasons(CP.analyze(CP.parse_pipeline(p)))["input:normalize:raw"], ("INSUFFICIENT_EVIDENCE", "external_without_assumption"))

    def test_missing_tag_is_unestablished_not_contradicted(self):
        p = copy.deepcopy(load("good"))
        del p["activities"][0]["produces"][0]["tags"]["offsets"]
        self.assertEqual(reasons(CP.analyze(CP.parse_pipeline(p)))["input:index:text"], ("INSUFFICIENT_EVIDENCE", "tag_not_established"))

    def test_generation_equality_unknown_and_conflicting(self):
        self.assertEqual(reasons(analyze("generation-mismatch"))["generation:0"], ("CONTRADICTED", "generations_differ"))
        p = copy.deepcopy(load("generation-mismatch"))
        del p["activities"][1]["produces"][0]["generation"]
        self.assertEqual(reasons(CP.analyze(CP.parse_pipeline(p)))["generation:0"], ("INSUFFICIENT_EVIDENCE", "generation_unstated"))
        p["same_generation"] = [["x", "ghost"]]
        self.assertEqual(reasons(CP.analyze(CP.parse_pipeline(p)))["generation:0"], ("INSUFFICIENT_EVIDENCE", "artifact_not_declared"))

    def test_producer_collision_and_contradictory_internal_external_declaration_are_invalid(self):
        p = copy.deepcopy(load("good"))
        p["activities"].append({"id": "dup", "produces": [{"artifact": "text", "tags": {}, "generation": "g1"}]})
        self.assertEqual(reasons(CP.analyze(CP.parse_pipeline(p)))["graph:declarations"], ("INVALID_EVIDENCE", "producer_collision"))
        p = copy.deepcopy(load("good"))
        p["external"].append({"artifact": "text", "tags": {}, "assumption": "x"})
        self.assertEqual(reasons(CP.analyze(CP.parse_pipeline(p)))["graph:declarations"], ("INVALID_EVIDENCE", "internal_and_external"))

    def test_unrelated_branches_do_not_block_a_targeted_claim_but_are_not_called_verified(self):
        whole, scoped = analyze("scoped"), analyze("scoped", "report")
        self.assertEqual((whole["overall"], scoped["overall"]), ("INSUFFICIENT_EVIDENCE", "SUPPORTED"))
        self.assertEqual(scoped["scope"]["not_evaluated"], ["side"])
        self.assertIn("unresolved, not verified", scoped["scope"]["statement"])
        with self.assertRaises(CP.PipelineError):
            analyze("scoped", "ghost")

    def test_equivalent_permutations_give_identical_obligations_and_order(self):
        base = analyze("scoped")
        for seed in range(8):
            p = copy.deepcopy(load("scoped"))
            random.Random(seed).shuffle(p["activities"])
            for a in p["activities"]:
                random.Random(seed).shuffle(a["consumes"])
            self.assertEqual(CP.analyze(CP.parse_pipeline(p)), base)

    def test_each_obligation_keeps_its_own_finding_and_support_needs_all(self):
        a = analyze("scoped")
        self.assertGreater(len({o["verdict"] for o in a["obligations"]}), 1)
        self.assertEqual(a["overall"], "INSUFFICIENT_EVIDENCE")                          # one unresolved essential obligation blocks support


class Malformed(unittest.TestCase):
    def test_shapes_and_limits_are_invalid_evidence(self):
        bad = [[], {"activities": []}, {"activities": [{"id": "a"}, {"id": "a"}]}, {"activities": [{"id": "a", "evil": 1}]},
               {"activities": [{"id": "a", "consumes": [{"artifact": "x", "requires": {"unit": [1]}}]}]},
               {"activities": [{"id": "a", "depends_on": ["b", "b"]}]}, {"activities": [{"id": "a", "conversion": {"from": "x"}}]},
               {"activities": [{"id": "a"}], "same_generation": [["x"]]}, {"activities": [{"id": "a"}], "script": "x"},
               {"activities": [{"id": f"n{i}"} for i in range(501)]}]
        for p in bad:
            with self.assertRaises(CP.PipelineError, msg=str(p)[:60]):
                CP.parse_pipeline(p)

    def test_rule_reports_malformed_and_unreadable_evidence_as_invalid(self):
        lab = Lab({"d": ("json", json.dumps({"pipeline": {"activities": []}}).encode())})
        self.assertEqual(one(lab, lab.claim("composition.valid", "pipeline", {})).reason_code, "malformed_pipeline")
        lab = Lab({"d": ("json", b'{"pipeline": ')})
        f = one(lab, lab.claim("composition.valid", "pipeline", {}))
        self.assertEqual((f.verdict, f.reason_code), (Verdict.INVALID_EVIDENCE, "evidence_unreadable"))


class Impact(unittest.TestCase):
    def setUp(self):
        self.old = CP.parse_pipeline(load("good"))

    def test_changing_a_generation_invalidates_exactly_the_reachable_activities_with_paths(self):
        p = copy.deepcopy(load("good"))
        p["activities"][0]["produces"][0]["generation"] = "g2"
        res = CP.impact(self.old, CP.parse_pipeline(p))
        self.assertEqual(res["changed"], ["normalize"])
        self.assertEqual(res["affected"], {"index": ["normalize", "index"], "normalize": ["normalize"]})

    def test_descendant_and_unrelated_changes(self):
        p = copy.deepcopy(load("scoped"))
        old = CP.parse_pipeline(p)
        q = copy.deepcopy(p)
        q["activities"][2]["produces"][0]["generation"] = "g9"            # the unrelated 'side' branch
        res = CP.impact(old, CP.parse_pipeline(q))
        self.assertEqual(res["changed"], ["side"])
        self.assertEqual(sorted(res["affected"]), ["side"])
        self.assertEqual(sorted(res["unaffected"]), ["normalize", "report"])
        r = copy.deepcopy(p)
        r["activities"][1]["produces"][0]["generation"] = "g9"            # a descendant: nothing upstream is touched
        self.assertEqual(sorted(CP.impact(old, CP.parse_pipeline(r))["affected"]), ["report"])

    def test_external_change_reaches_its_consumers_and_additions_are_listed(self):
        p = copy.deepcopy(load("good"))
        p["external"][0]["tags"]["unit"] = "milliseconds"
        p["activities"].append({"id": "extra", "produces": [{"artifact": "e", "tags": {}, "generation": "g1"}]})
        res = CP.impact(self.old, CP.parse_pipeline(p))
        self.assertEqual((res["external_changed"], res["added"]), (["raw"], ["extra"]))
        self.assertEqual(res["affected"]["index"], ["normalize", "index"])


class ProducerCheckerDifferential(unittest.TestCase):
    """Producer (composition.py) and the checker's own implementation must agree on random pipelines."""

    def random_pipeline(self, rng):
        n = rng.randint(1, 6)
        arts = [f"a{i}" for i in range(n + 2)]
        units = ["seconds", "milliseconds"]
        ext = [{"artifact": a, "tags": {"unit": rng.choice(units)}, "generation": rng.choice(["g1", "g2"]),
                **({"assumption": "x"} if rng.random() < 0.8 else {})} for a in arts[:2] if rng.random() < 0.8]
        acts = []
        for i in range(n):
            consumes = [{"artifact": rng.choice(arts), "requires": rng.choice([{}, {"unit": rng.choice(units)}, {"unit": {"in": units}}, {"mode": "x"}])}
                        for _ in range(rng.randint(0, 2))]
            produces = [{"artifact": arts[i + 2] if rng.random() < 0.9 else rng.choice(arts), "tags": rng.choice([{}, {"unit": rng.choice(units)}]),
                         **({"generation": rng.choice(["g1", "g2"])} if rng.random() < 0.8 else {})}]
            a = {"id": f"act{i}", "consumes": consumes, "produces": produces,
                 "depends_on": [f"act{rng.randint(0, n)}" for _ in range(rng.randint(0, 1))]}
            if rng.random() < 0.2:
                a["conversion"] = rng.choice([{"from": "milliseconds", "to": "seconds", "factor": "0.001"}, {"from": "seconds", "to": "milliseconds", "factor": "999"},
                                              {"from": "x", "to": "y", "factor": "1"}, {"from": "seconds", "to": "milliseconds", "factor": "oops"}])
            acts.append(a)
        p = {"external": ext, "activities": acts}
        if rng.random() < 0.4:
            p["same_generation"] = [rng.sample(arts, 2)]
        return p

    def test_random_pipelines_agree(self):
        rng = random.Random(11)
        seen = {}
        for _ in range(500):
            raw = self.random_pipeline(rng)
            target = rng.choice([None, None, f"act{rng.randint(0, 3)}"])
            try:
                mine = CP.analyze(CP.parse_pipeline(copy.deepcopy(raw)), target)
                mine_obs = [(o["id"], o["verdict"], o["reason"]) for o in mine["obligations"]]
                mine_overall = mine["overall"]
            except CP.PipelineError:
                mine_obs = mine_overall = "ERR"
            try:
                obs, overall = checker._pipeline_obligations(copy.deepcopy(raw), target)
            except ValueError:
                obs = overall = "ERR"
            self.assertEqual((mine_overall, mine_obs), (overall, obs), (raw, target))
            seen[mine_overall] = seen.get(mine_overall, 0) + 1
        for v in ("SUPPORTED", "CONTRADICTED", "INSUFFICIENT_EVIDENCE"):
            self.assertGreater(seen.get(v, 0), 10, seen)          # the generator exercises every outcome


if __name__ == "__main__":
    unittest.main()

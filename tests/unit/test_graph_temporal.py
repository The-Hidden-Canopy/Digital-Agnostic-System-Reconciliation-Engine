import random
import unittest
from decimal import Decimal as D

from tests.unit.test_rules_engine import Lab, one
from verifier_lab import temporal as T
from verifier_lab.graph import (find_cycle, inspect_bundle, invalidation_paths, stable_topological_order)
from verifier_lab.model import Activity, Artifact, Bundle, Verdict

I = lambda a, b, c="s": T.Interval(D(a), D(b), c)
A = lambda s, e, c="s": T.Activity(I(*s, c), I(*e, c))


def bundle(acts, arts=()):
    return Bundle.build(arts, [], acts, [], [])


class Ordering(unittest.TestCase):
    def test_chain_diamond_disconnected_and_shuffle_invariance(self):
        edges = [("a", "b"), ("a", "c"), ("b", "d"), ("c", "d")]
        nodes = list("abcdxy")
        want, residual = stable_topological_order(nodes, edges)
        self.assertEqual((want, residual), (tuple("abcdxy"), ()))
        for seed in range(8):
            n, e = nodes[:], edges[:]
            random.Random(seed).shuffle(n)
            random.Random(seed).shuffle(e)
            self.assertEqual(stable_topological_order(n, e)[0], want)

    def test_cycle_witness_is_a_real_cycle_not_every_residual_node(self):
        edges = [("a", "b"), ("b", "c"), ("c", "b"), ("c", "z")]     # z is downstream of the cycle, not on it
        order, residual = stable_topological_order("abcz", edges)
        self.assertEqual(order, ("a",))
        cyc, trunc = find_cycle(residual, edges)
        self.assertEqual((cyc[0], cyc[-1], trunc), (cyc[-1], cyc[0], False))
        self.assertEqual(set(cyc), {"b", "c"})
        self.assertIn("z", residual)
        self.assertEqual(find_cycle(*stable_topological_order("ab", [("a", "b")])[1:], [("a", "b")]), (None, False))

    def test_self_cycle_and_truncation(self):
        order, residual = stable_topological_order("a", [("a", "a")])
        self.assertEqual(find_cycle(residual, [("a", "a")])[0], ("a", "a"))
        ring = [(str(i), str((i + 1) % 200)) for i in range(200)]
        cyc, trunc = find_cycle([str(i) for i in range(200)], ring, limit=10)
        self.assertTrue(trunc)
        self.assertEqual(len(cyc), 11)

    def test_invalidation_paths_are_exact(self):
        edges = [("a", "b"), ("b", "c"), ("a", "d"), ("x", "y")]
        paths = invalidation_paths(edges, "a")
        self.assertEqual(paths, {"b": ("a", "b"), "d": ("a", "d"), "c": ("a", "b", "c")})
        self.assertEqual(invalidation_paths(edges, "c"), {})            # descendant change affects nothing upstream
        self.assertNotIn("y", paths)                                    # unrelated branch untouched


class BundleInspection(unittest.TestCase):
    def arts(self, n):
        return [Artifact.make("sha256:" + str(i) * 64, "text/plain", 1, f"artifacts/{i}") for i in range(n)]

    def test_missing_input_is_not_silently_external_and_acyclic_is_not_closed(self):
        a0, a1 = self.arts(2)
        ghost = "activity:sha256:" + "f" * 64
        act = Activity.make("op", [a0.id], [a1.id], [], "g")
        ins = inspect_bundle(bundle([act], [a0, a1]))
        self.assertTrue(ins.acyclic)
        self.assertFalse(ins.closed)
        self.assertEqual(ins.to_dict()["missing"][0]["required"], a0.id)
        self.assertTrue(inspect_bundle(bundle([act], [a0, a1]), frozenset({a0.id})).closed)

    def test_producer_collision_and_conflicting_declaration(self):
        a0, a1 = self.arts(2)
        p1 = Activity.make("p1", [], [a1.id], [], "g1")
        p2 = Activity.make("p2", [], [a1.id], [], "g2")
        c = Activity.make("c", [a1.id], [], [], "g")
        ins = inspect_bundle(bundle([p1, p2, c], [a0, a1]), frozenset({a1.id}))
        codes = {p["code"] for p in ins.problems}
        self.assertEqual(codes, {"producer_collision", "conflicting_declaration"})

    def test_chain_via_artifacts_orders_producer_before_consumer(self):
        a0, a1, a2 = self.arts(3)
        p = Activity.make("p", [a0.id], [a1.id], [], "g")
        c = Activity.make("c", [a1.id], [a2.id], [], "g")
        ins = inspect_bundle(bundle([c, p], [a0, a1, a2]), frozenset({a0.id}))
        self.assertEqual(ins.order, tuple(sorted([p.id, c.id], key=lambda x: 0 if x == p.id else 1)))
        self.assertTrue(ins.closed and ins.acyclic)


class Comparators(unittest.TestCase):
    def test_strict_precedence_needs_strict_gap(self):
        self.assertEqual(T.definitely_before(I(1, 2), I(3, 4)), T.Tri.YES)
        self.assertEqual(T.definitely_before(I(1, 3), I(3, 4)), T.Tri.UNKNOWN)   # touching closed intervals
        self.assertEqual(T.definitely_before(I(2, 5), I(3, 4)), T.Tri.UNKNOWN)
        self.assertEqual(T.definitely_before(I(5, 6), I(1, 2)), T.Tri.NO)
        self.assertEqual(T.definitely_before(I(2, 2), I(2, 2)), T.Tri.NO)
        self.assertEqual(T.definitely_before(I(1, 2, "a"), I(3, 4, "b")), T.Tri.UNKNOWN)   # different clocks

    def test_half_open_overlap_and_uncertainty(self):
        self.assertEqual(T.positive_overlap(A((0, 0), (5, 5)), A((3, 3), (8, 8))), T.Tri.YES)
        self.assertEqual(T.positive_overlap(A((0, 0), (5, 5)), A((5, 5), (8, 8))), T.Tri.NO)       # boundary touch
        self.assertEqual(T.positive_overlap(A((0, 1), (5, 6)), A((4, 5), (8, 9))), T.Tri.UNKNOWN)
        self.assertEqual(T.positive_overlap(A((0, 0), (5, 5), "x"), A((3, 3), (8, 8), "y")), T.Tri.UNKNOWN)
        with self.assertRaises(ValueError):
            A((5, 5), (1, 1))

    def test_equality_overlap_is_unknown_not_equal(self):
        self.assertEqual(T.values_equal(I(1, 3), I(2, 4)), T.Tri.UNKNOWN)
        self.assertEqual(T.values_equal(I(1, 2), I(3, 4)), T.Tri.NO)
        self.assertEqual(T.values_equal(I(2, 2), I(2, 2)), T.Tri.YES)
        with self.assertRaises(ValueError):
            T.Interval(D("NaN"), D(1))
        self.assertEqual(T.values_equal(I("-0", "-0"), I("0", "0")), T.Tri.YES)    # signed zero

    def test_logical_clocks_possibility_sets_and_happens_before(self):
        self.assertEqual(T.logical_before(T.LogicalTime("a", 1), T.LogicalTime("a", 2)), T.Tri.YES)
        self.assertEqual(T.logical_before(T.LogicalTime("a", 1), T.LogicalTime("b", 2)), T.Tri.UNKNOWN)
        self.assertEqual(T.possibility_sets_compatible(frozenset("ab"), frozenset("cd")), T.Tri.NO)
        self.assertEqual(T.possibility_sets_compatible(frozenset("ab"), frozenset("bc")), T.Tri.UNKNOWN)
        with self.assertRaises(ValueError):
            T.possibility_sets_compatible(frozenset(), frozenset("a"))
        edges = [("a", "b"), ("b", "c")]
        self.assertEqual(T.happens_before_reachable(edges, "a", "c"), T.Tri.YES)
        self.assertEqual(T.happens_before_reachable(edges, "c", "a"), T.Tri.UNKNOWN)   # no path != reversed or simultaneous


def rel(lab, left, right, relation, coord="s"):
    return one(lab, lab.claim("interval.relation", f"{left}/{right}",
                              {"left": left, "right": right, "relation": relation, "coord": coord}))


class IntervalRule(unittest.TestCase):
    def lab(self, **kv):
        import json
        return Lab({"sched": ("json", json.dumps({"a": {"start": 0, "end": 5}, "b": {"start": 5, "end": 9}, **kv}).encode())})

    def test_exact_bounds(self):
        lab = self.lab()
        self.assertEqual(rel(lab, "a", "b", "precedes").verdict, Verdict.SUPPORTED)
        self.assertEqual(rel(lab, "a", "b", "no_overlap").verdict, Verdict.SUPPORTED)     # touching half-open: no overlap
        f = rel(lab, "a", "b", "overlaps")
        self.assertEqual((f.verdict, f.reason_code), (Verdict.CONTRADICTED, "relation_violated"))
        self.assertEqual(f.witness["bounds"]["a.end"], {"lo": "5", "hi": "5", "coord": "s"})
        lab = self.lab(c={"start": 3, "end": 8})
        self.assertEqual(rel(lab, "a", "c", "overlaps").verdict, Verdict.SUPPORTED)
        self.assertEqual(rel(lab, "a", "c", "precedes").verdict, Verdict.CONTRADICTED)

    def test_two_observed_durations_never_prove_overlap(self):
        lab = Lab({"d": ("json", b'{"a": {"duration": 5}, "b": {"duration": 4}}')})
        f = rel(lab, "a", "b", "overlaps")
        self.assertEqual((f.verdict, f.reason_code), (Verdict.INSUFFICIENT_EVIDENCE, "bound_missing"))

    def test_uncertain_bounds_leave_overlap_insufficient_with_what_is_needed(self):
        lab = Lab({"s": ("json", b'{"a":{"start":0,"end_min":4,"end_max":7},"b":{"start_min":5,"start_max":6,"end":9}}')})
        f = rel(lab, "a", "b", "overlaps")
        self.assertEqual((f.verdict, f.reason_code), (Verdict.INSUFFICIENT_EVIDENCE, "relation_uncertain"))
        self.assertIn("tighter bounds", f.witness["needed"])
        lab = Lab({"s": ("json", b'{"a":{"start":0,"end_min":4,"end_max":5},"b":{"start_min":5,"start_max":6,"end":9}}')})
        self.assertEqual(rel(lab, "a", "b", "precedes").verdict, Verdict.SUPPORTED)

    def test_invalid_and_conflicting_evidence(self):
        self.assertEqual(rel(Lab({"s": ("json", b'{"a":{"start":9,"end":1},"b":{"start":0,"end":1}}')}), "a", "b", "precedes").verdict,
                         Verdict.INVALID_EVIDENCE)
        self.assertEqual(rel(Lab({"s": ("json", b'{"a":{"start":"x","end":1},"b":{"start":0,"end":1}}')}), "a", "b", "precedes").verdict,
                         Verdict.HUMAN_REVIEW_REQUIRED)
        lab = Lab({"p": ("json", b'{"a":{"start":0,"end":5},"b":{"start":5,"end":9}}'),
                   "q": ("json", b'{"a":{"start":0,"end":6},"b":{"start":5,"end":9}}')})
        f = rel(lab, "a", "b", "precedes")
        self.assertEqual((f.verdict, f.reason_code), (Verdict.HUMAN_REVIEW_REQUIRED, "evidence_conflict"))   # never pick a winner

    def test_malformed_arguments(self):
        lab = self.lab()
        for args in ({}, {"left": "a", "right": "a", "relation": "precedes", "coord": "s"},
                     {"left": "a", "right": "b", "relation": "causes", "coord": "s"},
                     {"left": "a", "right": "b", "relation": "precedes"}):
            self.assertEqual(one(lab, lab.claim("interval.relation", "x", args)).verdict, Verdict.INVALID_EVIDENCE)


if __name__ == "__main__":
    unittest.main()


class ProducerCheckerDifferential(unittest.TestCase):
    """Producer (temporal.py) and checker (own corner-case formulation) must agree on random cases."""

    def test_random_interval_cases_agree(self):
        import json
        from verifier_lab import checker
        rng = random.Random(7)
        n_by_verdict = {}
        for _ in range(400):
            doc, obs = {}, []
            for name in ("a", "b"):
                s0 = rng.randint(0, 8)
                e0 = s0 + rng.randint(0, 6)
                rec = {"start": s0, "end": e0}
                if rng.random() < 0.5:        # uncertain bounds around the point values
                    rec = {"start_min": s0 - rng.randint(0, 2), "start_max": s0 + rng.randint(0, 2),
                           "end_min": e0 - rng.randint(0, 2), "end_max": e0 + rng.randint(0, 2)}
                if rng.random() < 0.1:
                    rec.pop(next(iter(rec)))   # drop a bound: must not be guessed
                doc[name] = rec
            lab = Lab({"s": ("json", json.dumps(doc).encode())})
            args = {"left": "a", "right": "b", "relation": rng.choice(["precedes", "no_overlap", "overlaps"]), "coord": "s"}
            f = one(lab, lab.claim("interval.relation", "x", args))
            for name, rec in doc.items():
                for k, v in rec.items():
                    obs.append({"key": f"{name}.{k}", "value": {"kind": "integer", "value": str(v)}})
            verdict, reason, _ = checker._rederive_interval(args, obs, [], [])
            self.assertEqual((f.verdict.value, f.reason_code), (verdict, reason), (doc, args))
            n_by_verdict[verdict] = n_by_verdict.get(verdict, 0) + 1
        for v in ("SUPPORTED", "CONTRADICTED", "INSUFFICIENT_EVIDENCE"):
            self.assertGreater(n_by_verdict.get(v, 0), 10, n_by_verdict)     # the generator actually exercises every outcome

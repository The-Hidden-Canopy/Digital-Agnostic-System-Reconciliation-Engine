import json
import shutil
import tempfile
import unittest
from pathlib import Path

from verifier_lab.canonical import canonical_bytes, sha256_digest
from verifier_lab.experiments.checkpoint import ExperimentCase, run_experiment
from verifier_lab.experiments.history import generate_history
from verifier_lab.experiments.package import (CheckpointOracle, LaxOracle, build_package, verify_in_process,
                                              verify_package_fresh_process, write_package)
from verifier_lab.minimize import OracleOutcome, ReductionBudget, minimize
from verifier_lab.reducers.graph import (CycleOracle, DeleteEdges, DeleteNodesRejectingDependents,
                                         DeleteNodesWithClosure, GraphCase)
from verifier_lab.reducers.sequence import DeleteTransactionMembers, DeleteUnits
from verifier_lab.reducers.values import SimplifyValues

P = lambda k, v="x": {"op": "put", "key": k, "value": v}
D = lambda k: {"op": "delete", "key": k}
AL = lambda k: {"op": "allocate", "key": k}
TX = lambda *m: {"op": "txn", "ops": list(m)}
REDUCERS = lambda: [DeleteUnits(), DeleteTransactionMembers(), SimplifyValues()]


def long_history(defect="reset_next_id", n=80):
    """Many irrelevant writes around one relevant allocate-delete-(checkpoint)-restore sequence."""
    noise = [P(f"k{i % 7}", "v" * (i % 5)) for i in range(n)]
    ops = noise[:30] + [AL("hot"), D("hot")] + noise[30:60]
    return ExperimentCase("seed", tuple(ops), 32, (defect,))


class SequenceReduction(unittest.TestCase):
    def test_reduces_noise_and_preserves_the_failure_identity(self):
        case = long_history()
        r = minimize(case, CheckpointOracle(), REDUCERS())
        self.assertEqual(r.status, "REDUCED")
        self.assertLessEqual(len(r.best.ops), 3)
        self.assertEqual(run_experiment(r.best).first().field, "next_identifier")
        self.assertEqual(CheckpointOracle().signature_of(r.best), r.signature)
        self.assertEqual(r.reproduction_outcomes, ["same_failure"] * 3)

    def test_result_is_deterministic(self):
        a = minimize(long_history(), CheckpointOracle(), REDUCERS())
        b = minimize(long_history(), CheckpointOracle(), REDUCERS())
        self.assertEqual((a.best.identity(), a.log), (b.best.identity(), b.log))

    def test_every_accepted_step_strictly_lowers_the_cost(self):
        case = long_history()
        r = minimize(case, CheckpointOracle(), REDUCERS())
        self.assertLess(r.best.cost(), case.cost())
        self.assertTrue(all(e["outcome"] == "same_failure" for e in r.log if e["accepted"]))

    def test_one_minimality_by_brute_force(self):
        r = minimize(long_history(), CheckpointOracle(), REDUCERS())
        orc = CheckpointOracle()
        orc.bind(r.signature)
        for reducer in REDUCERS():
            for label, cand in reducer.proposals(r.best):
                if cand.cost() < r.best.cost():
                    self.assertNotEqual(orc.evaluate(cand), OracleOutcome.SAME_FAILURE, (reducer.name, label))

    def test_setup_deletion_is_never_counted_as_success(self):
        case = ExperimentCase("s", (AL("a"), D("a"), P("x")), 2, ("reset_next_id",))
        orc = CheckpointOracle()
        orc.bind(orc.signature_of(case))
        no_setup = ExperimentCase("s", (D("a"), P("x")), 1, ("reset_next_id",))          # allocate removed
        self.assertEqual(orc.evaluate(no_setup), OracleOutcome.NO_FAILURE)
        out_of_range = ExperimentCase("s", (P("x"),), 5, ("reset_next_id",))
        self.assertEqual(orc.evaluate(out_of_range), OracleOutcome.INVALID_CANDIDATE)
        different = ExperimentCase("s", (AL("a"), D("a"), P("x")), 2, ("omit_random_cursor", "double_cursor_after_restore"))
        self.assertEqual(orc.evaluate(different), OracleOutcome.DIFFERENT_FAILURE)

    def test_transactions_are_deleted_whole_and_members_by_the_dedicated_reducer(self):
        case = ExperimentCase("s", (P("a"), TX(P("b"), AL("c"), D("zzz")), P("d")), 0, ("flatten_txn",))
        for _, cand in DeleteUnits().proposals(case):
            for op in cand.ops:
                if op["op"] == "txn":
                    self.assertEqual(len(op["ops"]), 3)                   # never split by the default reducer
        r = minimize(case, CheckpointOracle(), REDUCERS())
        self.assertEqual(r.status, "REDUCED")
        self.assertEqual(len(r.best.ops), 1)
        self.assertEqual(r.best.ops[0]["op"], "txn")                      # atomicity retained: flattening would lose the bug
        self.assertEqual(len(r.best.ops[0]["ops"]), 2)                    # one state-changing member + the failing delete
        self.assertEqual(r.best.ops[0]["ops"][-1]["op"], "delete")

    def test_boundary_tracks_the_same_position_after_deletions(self):
        case = ExperimentCase("s", (P("a"), P("b"), P("c"), P("d")), 3)
        drop = [c for lbl, c in DeleteUnits().proposals(case) if lbl.startswith("delete_chunk[0:2]")][0]
        self.assertEqual((len(drop.ops), drop.boundary), (2, 1))

    def test_empty_and_single_operation_cases(self):
        for ops, boundary in (((), 0), ((P("a"),), 0)):
            r = minimize(ExperimentCase("s", ops, boundary, ("sort_replay",)), CheckpointOracle(), REDUCERS())
            self.assertEqual(r.status, "NOT_REPRODUCED")                  # nothing fails: no minimality claim is made
        r = minimize(ExperimentCase("s", (TX(P("a"), D("zz")), ), 0, ("flatten_txn",)), CheckpointOracle(), REDUCERS())
        self.assertEqual((r.status, len(r.best.ops)), ("REDUCED", 1))

    def test_budget_exhaustion_returns_best_so_far_with_honest_scope(self):
        r = minimize(long_history(), CheckpointOracle(), REDUCERS(), ReductionBudget(max_oracle_calls=8))
        self.assertEqual((r.status, r.stop_reason), ("BEST_SO_FAR", "oracle_call_budget_exhausted"))
        self.assertIn("further reduction may be possible", r.scope_statement(REDUCERS()))
        self.assertEqual(r.exhausted_reducers, [])
        self.assertLessEqual(r.oracle_calls, 8)

    def test_oracle_cache_prevents_duplicate_evaluation(self):
        calls = []

        class Counting(CheckpointOracle):
            def evaluate(self, case):
                calls.append(case.identity())
                return super().evaluate(case)
        class Twice:                                        # proposes the same (non-failing) candidate twice
            name, description = "twice", "duplicate proposals"

            def proposals(self, case):
                smaller = ExperimentCase(case.seed, case.ops[:-1], min(case.boundary, len(case.ops) - 1), case.defects)
                yield "a", smaller
                yield "b", smaller
        case = ExperimentCase("s", (AL("h"), D("h"), P("k", "v")), 2, ("reset_next_id",))
        r = minimize(case, Counting(), [Twice()])
        self.assertEqual((r.log[0]["outcome"], r.cache_hits), ("no_failure", 1))    # the second identical proposal hit the cache
        later = calls[3:]                                                  # after the 3 reproduction runs
        self.assertEqual(len(later), len(set(later)))

    def test_unstable_oracle_is_unresolved_not_hidden(self):
        class Flaky(CheckpointOracle):
            n = 0

            def evaluate(self, case):
                Flaky.n += 1
                return OracleOutcome.SAME_FAILURE if Flaky.n % 2 else OracleOutcome.NO_FAILURE
        r = minimize(long_history(), Flaky(), REDUCERS())
        self.assertEqual(r.status, "UNSTABLE")
        self.assertEqual(r.best, r.original)

    def test_operational_errors_are_not_cached_or_accepted(self):
        class Crashy(CheckpointOracle):
            def evaluate(self, case):
                if len(case.ops) < 30 and len(case.ops) != 3:
                    raise TimeoutError("transient")
                return super().evaluate(case)
        r = minimize(long_history(), Crashy(), REDUCERS(), ReductionBudget(max_oracle_calls=200))
        self.assertGreater(len(r.best.ops), 3)                              # transient errors never count as a reproduction


class GraphReduction(unittest.TestCase):
    def test_cyclic_graph_shrinks_to_a_valid_cycle_without_dangling_references(self):
        nodes = tuple("abcdefgh")
        edges = (("a", "b"), ("b", "c"), ("c", "d"), ("d", "b"), ("c", "e"), ("e", "f"), ("g", "h"), ("a", "g"))
        reducers = [DeleteEdges(), DeleteNodesWithClosure(), DeleteNodesRejectingDependents()]
        r = minimize(GraphCase(nodes, edges), CycleOracle(), reducers)
        self.assertEqual(r.status, "REDUCED")
        self.assertEqual(set(r.best.nodes), {"b", "c", "d"})
        self.assertEqual(set(r.best.edges), {("b", "c"), ("c", "d"), ("d", "b")})
        self.assertEqual(CycleOracle().evaluate(r.best), OracleOutcome.SAME_FAILURE)

    def test_dangling_edges_are_invalid_candidates_and_acyclic_input_is_not_reducible(self):
        self.assertEqual(CycleOracle().evaluate(GraphCase(("a",), (("a", "zz"),))), OracleOutcome.INVALID_CANDIDATE)
        r = minimize(GraphCase(("a", "b"), (("a", "b"),)), CycleOracle(), [DeleteEdges()])
        self.assertEqual(r.status, "NOT_REPRODUCED")

    def test_node_deletion_operators_are_distinct_in_the_log(self):
        r = minimize(GraphCase(tuple("abc"), (("a", "b"), ("b", "a"), ("b", "c"))), CycleOracle(),
                     [DeleteNodesWithClosure(), DeleteEdges()])
        self.assertTrue({e["reducer"] for e in r.log} <= {"delete_nodes_with_closure", "delete_edges"})
        self.assertEqual(set(r.best.nodes), {"a", "b"})


class CounterexamplePackage(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vl-pkg-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.result = minimize(long_history(), CheckpointOracle(), REDUCERS())
        self.pkg = build_package(self.result, REDUCERS())
        self.path = self.tmp / "pkg.json"
        write_package(self.pkg, self.path)

    def test_fresh_process_reproduces_and_scope_is_stated(self):
        res = verify_package_fresh_process(self.path)
        self.assertEqual(res["status"], "VERIFIED", [c for c in res["checks"] if not c["ok"]])
        self.assertIn("not global minimality", self.pkg["reduction"]["scope"])
        self.assertEqual(self.pkg["witness"]["field"], "next_identifier")

    def test_refuses_to_overwrite(self):
        with self.assertRaises(FileExistsError):
            write_package(self.pkg, self.path)

    def tampered(self, mutate):
        body = json.loads(json.dumps({k: v for k, v in self.pkg.items() if k != "id"}))
        mutate(body)
        return {**body, "id": sha256_digest(canonical_bytes(body))}        # attacker recomputes the package id

    def test_edited_case_or_signature_is_rejected_even_with_a_recomputed_package_id(self):
        def make_pass(b):                                                    # claim a failure the case does not have
            b["reduced"]["case"]["defects"] = []
            b["reduced"]["identity"] = ExperimentCase.from_dict(b["reduced"]["case"]).identity()
        res = verify_in_process(self.tampered(make_pass))
        self.assertEqual(res["status"], "REJECTED")
        self.assertIn("reduced_case_fails_with_same_signature", {c["name"] for c in res["checks"] if not c["ok"]})

        def other_sig(b):
            b["signature"]["witness_class"] = "resumed:random_cursor"
        self.assertEqual(verify_in_process(self.tampered(other_sig))["status"], "REJECTED")

        def forged_witness(b):
            b["witness"]["actual"] = 12345
        self.assertIn("witness_reproduces", {c["name"] for c in verify_in_process(self.tampered(forged_witness))["checks"] if not c["ok"]})

    def test_unmodified_package_id_mismatch_and_semantics_version(self):
        bad = dict(self.pkg, status="REDUCED!")
        self.assertIn("package_identity", {c["name"] for c in verify_in_process(bad)["checks"] if not c["ok"]})
        res = verify_in_process(self.tampered(lambda b: b["runner"].update(semantics_version="v0")))
        self.assertIn("semantics_version", {c["name"] for c in res["checks"] if not c["ok"]})

    def test_a_lax_reducer_that_accepts_any_failure_is_caught_by_the_signature(self):
        """Conformance: an oracle that treats any divergence as the failure produces output the verifier rejects."""
        case = ExperimentCase("s", (AL("a"), D("a"), TX(P("x"), D("missing")), P("y")), 2,
                              ("reset_next_id", "flatten_txn"))
        orc = CheckpointOracle()
        sig = orc.signature_of(case)
        self.assertEqual(sig.witness_class, "continuous:entries")            # flatten_txn shows up first (continuous path)
        lax = minimize(case, LaxOracle(), REDUCERS(), signature=sig)
        strict = minimize(case, CheckpointOracle(), REDUCERS())
        self.assertEqual(strict.status, "REDUCED")
        pkg = build_package(lax, REDUCERS())
        res = verify_in_process(pkg)
        if CheckpointOracle().signature_of(lax.best) != sig:
            self.assertEqual(res["status"], "REJECTED")                     # the lax result drifted to a different failure
        self.assertEqual(verify_in_process(build_package(strict, REDUCERS()))["status"], "VERIFIED")


if __name__ == "__main__":
    unittest.main()

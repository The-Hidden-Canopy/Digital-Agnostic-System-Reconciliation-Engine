import unittest

from verifier_lab.detstream import DetStream
from verifier_lab.experiments import crash as crash_mod
from verifier_lab.experiments.checkpoint import ExperimentCase, run_experiment, state_diff
from verifier_lab.experiments.history import generate_history
from verifier_lab.replay import PlanError, make_checkpoint, plan_replay, restore_checkpoint
from verifier_lab.subjects import reference_store as ref
from verifier_lab.subjects.candidate_store import CandidateStore, DEFECTS

P = lambda k, v="x": {"op": "put", "key": k, "value": v}
D = lambda k: {"op": "delete", "key": k}
AL = lambda k: {"op": "allocate", "key": k}
DR = lambda k: {"op": "draw", "key": k}
TX = lambda *m: {"op": "txn", "ops": list(m)}


class DetStreamTests(unittest.TestCase):
    def test_reproducible_unbiased_and_recorded(self):
        a, b = DetStream("s"), DetStream("s")
        self.assertEqual([a.randbelow(7) for _ in range(50)], [b.randbelow(7) for _ in range(50)])
        self.assertNotEqual([DetStream("t").randbelow(1000) for _ in range(5)], [a.randbelow(1000) for _ in range(5)])
        counts = [0] * 3
        ds = DetStream("bias")
        for _ in range(3000):
            counts[ds.randbelow(3)] += 1
        self.assertTrue(all(900 < c < 1100 for c in counts), counts)
        self.assertEqual(a.record()["version"], "sha256-counter-1")
        with self.assertRaises(ValueError):
            a.randbelow(0)
        self.assertEqual(generate_history("x", 30), generate_history("x", 30))


class ReferenceStore(unittest.TestCase):
    def test_counters_and_allocation_continuity(self):
        s = ref.StoreState()
        for op in (AL("a"), AL("b"), D("b"), AL("c")):
            s = ref.apply_operation(s, op, "seed").state
        self.assertEqual(dict(s.entries), {"a": "id-0", "c": "id-2"})        # id-1 is gone but never reused
        self.assertEqual((s.next_identifier, s.logical_tick, s.operation_cursor), (3, 4, 4))

    def test_rejected_operations_change_nothing_including_counters(self):
        s = ref.apply_operation(ref.StoreState(), P("a"), "s").state
        t = ref.apply_operation(s, D("nope"), "s")
        self.assertFalse(t.accepted)
        self.assertEqual(t.state, s)

    def test_transactions_are_atomic_and_one_unit(self):
        s0 = ref.StoreState()
        ok = ref.apply_operation(s0, TX(P("a"), AL("b"), DR("c")), "s")
        self.assertTrue(ok.accepted)
        self.assertEqual((ok.state.operation_cursor, ok.state.logical_tick, ok.state.next_identifier, ok.state.random_cursor), (1, 3, 1, 1))
        bad = ref.apply_operation(s0, TX(P("a"), AL("b"), D("missing")), "s")
        self.assertEqual((bad.accepted, bad.state), (False, s0))              # nothing leaks, counters included
        self.assertFalse(ref.apply_operation(s0, TX(), "s").accepted)

    def test_invalid_operations_are_errors_not_rejections(self):
        for op in ({"op": "txn", "ops": [TX(P("a"))]}, {"op": "mystery", "key": "a"}, {"op": "put", "key": "a"},
                   {"op": "put", "key": "", "value": "x"}, {"op": "txn"}):
            with self.assertRaises(ref.OperationError):
                ref.apply_operation(ref.StoreState(), op, "s")

    def test_random_stream_depends_on_cursor_not_just_seed(self):
        self.assertNotEqual(ref.draw_value("s", 0), ref.draw_value("s", 1))
        self.assertEqual(ref.draw_value("s", 5), ref.draw_value("s", 5))

    def test_state_inventory_is_enforced(self):
        with self.assertRaises(ValueError):
            ref.StoreState.from_dict({"entries": []})


class CheckpointAndPlans(unittest.TestCase):
    def cp(self, n=2):
        states = [ref.StoreState().to_dict()]
        return make_checkpoint(states[0], "s", "gen-0", n)

    def test_checkpoint_roundtrip_and_tamper(self):
        cp = self.cp()
        back = restore_checkpoint(cp.to_bytes())
        self.assertEqual((back.id, back.state), (cp.id, cp.state))
        with self.assertRaises(PlanError) as cm:
            restore_checkpoint(cp.to_bytes().replace(b'"s"', b'"t"', 1))
        self.assertEqual(cm.exception.code, "checkpoint_identity_mismatch")
        with self.assertRaises(PlanError):
            make_checkpoint({"entries": []}, "s", "g", 0)

    def test_plan_refuses_instead_of_repairing(self):
        cp = self.cp(2)
        ok = [dict(P("a"), id="op-2", position=2), dict(P("b"), id="op-3", position=3)]
        self.assertEqual(len(plan_replay(cp, ok).operations), 2)
        self.assertEqual(plan_replay(cp, ok).identity(), plan_replay(cp, ok).identity())     # pure
        cases = {"operation_gap": [dict(P("a"), id="op-2", position=2), dict(P("b"), id="op-4", position=4)],
                 "duplicate_or_missing_operation_id": [dict(P("a"), id="x", position=2), dict(P("b"), id="x", position=3)],
                 "invalid_operation": [dict({"op": "zap", "key": "a"}, id="op-2", position=2)]}
        for code, ops in cases.items():
            with self.assertRaises(PlanError) as cm:
                plan_replay(cp, ops)
            self.assertEqual(cm.exception.code, code)
        with self.assertRaises(PlanError) as cm:
            plan_replay(cp, ok, expected_generation="gen-9")
        self.assertEqual(cm.exception.code, "wrong_generation")
        with self.assertRaises(PlanError) as cm:
            plan_replay(cp, ok, semantics_version="other")
        self.assertEqual(cm.exception.code, "incompatible_version")

    def test_empty_suffix_and_empty_history(self):
        self.assertEqual(plan_replay(self.cp(0), []).operations, ())
        self.assertTrue(run_experiment(ExperimentCase("s", (), 0)).passed)


HIST = (AL("a"), AL("b"), D("b"), DR("r"), TX(P("x", "1"), D("nope")), P("z", "q"), AL("c"), DR("s"), P("a", "w"))


class ThreeWayExperiment(unittest.TestCase):
    def test_correct_subject_matches_reference_for_every_boundary_and_seed(self):
        for seed in ("s1", "s2"):
            ops = tuple(generate_history(seed, 40))
            for boundary in (0, 1, 13, 39, 40):                       # start, end and interior checkpoints
                r = run_experiment(ExperimentCase(seed, ops, boundary))
                self.assertTrue(r.passed, (seed, boundary, r.to_dict()["divergences"][:1]))
                steps = r.coverage["steps_compared_per_path"]
                self.assertEqual(steps["resumed"], 40 - boundary)      # every post-boundary step compared, not just the last
                self.assertEqual(steps["replayed"], 40 - boundary)

    def test_each_defect_yields_its_field_level_witness(self):
        expect = {"omit_random_cursor": ("resumed", "random_cursor"), "reset_next_id": ("resumed", "next_identifier"),
                  "double_cursor_after_restore": ("resumed", "operation_cursor"),
                  "flatten_txn": ("continuous", "entries[x]"), "sort_replay": ("replayed", "observations")}
        self.assertEqual(set(expect), set(DEFECTS))
        for defect, (path, field) in expect.items():
            r = run_experiment(ExperimentCase("s1", HIST, 4, (defect,)))
            d = r.first()
            self.assertEqual((d.path, d.field), (path, field), defect)
            self.assertIsNotNone(d.op_index)

    def test_restore_only_defects_do_not_show_up_in_the_continuous_path(self):
        r = run_experiment(ExperimentCase("s1", HIST, 4, ("reset_next_id",)))
        self.assertIsNone(r.first("continuous"))
        self.assertIsNotNone(r.first("resumed"))
        self.assertIsNotNone(r.first("replayed"))

    def test_final_state_equality_can_hide_a_divergence_so_every_step_is_compared(self):
        # Reordering allocations changes which id each key receives, yet a coarse final-state check could miss it.
        r = run_experiment(ExperimentCase("s", (AL("b"), AL("a"), P("z", "1")), 0, ("sort_replay",)))
        self.assertFalse(r.passed)

    def test_boundary_and_invalid_operations_are_errors(self):
        self.assertEqual(run_experiment(ExperimentCase("s", HIST, 99)).errors[0]["code"], "boundary_out_of_range")
        self.assertEqual(run_experiment(ExperimentCase("s", ({"op": "zap", "key": "a"},), 0)).errors[0]["code"], "invalid_operation")

    def test_unknown_defects_refused_and_state_diff_order_is_fixed(self):
        with self.assertRaises(ValueError):
            CandidateStore("s", frozenset({"nope"}))
        a = ref.StoreState().to_dict()
        b = dict(a, logical_tick=5, next_identifier=3)
        self.assertEqual(state_diff(a, b), ("next_identifier", 0, 3))       # fixed field order: deterministic first witness

    def test_witness_is_reproducible_without_the_runner_state(self):
        c = ExperimentCase("s1", HIST, 4, ("omit_random_cursor",))
        self.assertEqual(run_experiment(c).to_dict(), run_experiment(ExperimentCase.from_dict(c.to_dict())).to_dict())


class CrashModel(unittest.TestCase):
    def test_correct_writer_and_recovery_survive_every_boundary(self):
        r = crash_mod.crash_experiment()
        self.assertTrue(r.passed)
        self.assertEqual(r.coverage["mode"], "exhaustive")
        self.assertEqual(r.coverage["explored"], list(range(r.coverage["steps"] + 1)))

    def test_marker_before_flush_publishes_an_incomplete_generation(self):
        r = crash_mod.crash_experiment("marker_before_flush")
        self.assertFalse(r.passed)
        self.assertEqual({v["class"] for v in r.violations}, {"writer_published_incomplete_generation"})

    def test_join_members_recovery_returns_mixed_generation_at_some_boundary(self):
        r = crash_mod.crash_experiment(None, ("join_members",))
        self.assertIn("mixed_generation", {v["class"] for v in r.violations})

    def test_silent_fallback_is_flagged_when_marker_points_at_incomplete_generation(self):
        r = crash_mod.crash_experiment("marker_before_flush", ("silent_fallback",))
        self.assertIn("silent_fallback", {v["class"] for v in r.violations})

    def test_budget_truncation_is_declared_not_hidden(self):
        r = crash_mod.crash_experiment(max_positions=3)
        self.assertEqual(r.coverage["mode"], "truncated_by_budget")
        self.assertEqual(r.coverage["explored"], [0, 1, 2])
        self.assertEqual(r.coverage["not_explored"], [3, 4, 5, 6])

    def test_orphaned_staged_generation_is_not_a_committed_result(self):
        disk = crash_mod.VirtualDisk()
        for s in crash_mod.writer_steps("gen-1", {"a": b"1"}):
            crash_mod.apply_step(disk, s)
        for s in crash_mod.writer_steps("gen-2", {"a": b"2"})[:2]:      # staged and even flushed member, no marker
            crash_mod.apply_step(disk, s)
        out = crash_mod.recover_reference(disk.crash(), {})
        self.assertEqual(out["generation"], "gen-1")


if __name__ == "__main__":
    unittest.main()

"""Positive/negative controls and independent reference agreement for the other synthetic systems."""
import copy
import json
import random
import tempfile
import unittest
from pathlib import Path

from verifier_lab.canonical import canonical_bytes, sha256_digest
from verifier_lab.experiments.laboratory import (LabCase, SUBJECTS, generated_case, reduce_case,
                                                run_case, verify_counterexample)
from verifier_lab.experiments.package import verify_package_fresh_process, write_package


class Laboratory(unittest.TestCase):
    def test_correct_subjects_agree_at_all_checkpoint_boundaries(self):
        for system in SUBJECTS:
            for count in (0, 1, 4, 12):
                for boundary in range(count + 1):
                    case = generated_case(system, count=count, boundary=boundary)
                    with self.subTest(system=system, count=count, boundary=boundary):
                        result = run_case(case)
                        self.assertTrue(result.passed, result.to_dict())
                        self.assertEqual(result.coverage['steps_compared_per_path'],
                                         {'continuous': count, 'resumed': count - boundary, 'replayed': count - boundary})

    def test_every_injected_defect_has_witness_and_fresh_verified_reduction(self):
        with tempfile.TemporaryDirectory() as directory:
            for system, module in SUBJECTS.items():
                for defect in module.DEFECTS:
                    with self.subTest(system=system, defect=defect):
                        case = generated_case(system, count=12, defects=[defect])
                        result = run_case(case)
                        self.assertFalse(result.errors)
                        self.assertFalse(result.passed)
                        self.assertIsNotNone(result.first())
                        self.assertTrue(result.first().field.startswith('state.'))
                        summary, package = reduce_case(case, 200)
                        self.assertLess(summary['reduced_units'], summary['original_units'])
                        self.assertEqual(verify_counterexample(package)['status'], 'VERIFIED')
                        path = Path(directory) / f'{system}-{defect}.json'
                        write_package(package, path)
                        self.assertEqual(verify_package_fresh_process(path)['status'], 'VERIFIED')

    def test_forged_witness_or_weakened_failure_is_rejected_even_after_rehashing(self):
        _, package = reduce_case(generated_case('scheduler', count=8, defects=['reverse_ties']))
        for change in ('witness', 'defect', 'signature', 'version'):
            forged = copy.deepcopy(package)
            if change == 'witness':
                forged['witness']['actual'] = 'invented'
            elif change == 'defect':
                forged['reduced']['case']['defects'] = []
                forged['reduced']['identity'] = LabCase.from_dict(forged['reduced']['case']).identity()
            elif change == 'signature':
                forged['signature']['witness_class'] = 'any_failure'
            else:
                forged['semantics_version'] = 'future'
            forged['id'] = sha256_digest(canonical_bytes({k: v for k, v in forged.items() if k != 'id'}))
            with self.subTest(change=change):
                self.assertEqual(verify_counterexample(forged)['status'], 'REJECTED')

    def test_invalid_cases_and_operational_outcomes_are_not_passes(self):
        invalid = (LabCase('unknown', 's', (), 0), LabCase('pipeline', 's', (), True),
                   LabCase('scheduler', 's', ({'op': 'advance', 'ticks': -1},), 0),
                   LabCase('pipeline', 's', ({'op': 'execute', 'command': 'no'},), 0))
        for case in invalid:
            with self.subTest(case=case):
                result = run_case(case)
                self.assertFalse(result.passed)
                self.assertTrue(result.errors)
                self.assertFalse(result.divergences)
        for count in (True, -1, 401, '5'):
            with self.assertRaises(ValueError):
                generated_case('scheduler', count=count)

    def test_correct_subject_has_no_counterexample_and_budget_is_explicit(self):
        summary, package = reduce_case(generated_case('pipeline', count=12))
        self.assertEqual(summary['status'], 'NOT_REPRODUCED')
        self.assertIsNone(package)
        summary, package = reduce_case(generated_case('pipeline', count=12, defects=['stale_cache']), 3)
        self.assertEqual(summary['status'], 'BEST_SO_FAR')
        self.assertEqual(summary['oracle_calls'], 3)
        self.assertIn('best', summary['scope'].lower())
        self.assertEqual(verify_counterexample(package)['status'], 'VERIFIED')

    def test_scheduler_handwritten_order_cancellation_and_rejection(self):
        ops = ({'op': 'schedule', 'key': 'later', 'delay': 2}, {'op': 'schedule', 'key': 'first', 'delay': 1},
               {'op': 'schedule', 'key': 'first', 'delay': 3}, {'op': 'cancel', 'key': 'later'},
               {'op': 'cancel', 'key': 'absent'}, {'op': 'advance', 'ticks': 5})
        module = SUBJECTS['scheduler']
        state = module.initial()
        for op in ops:
            state, _, _ = module.reference_step(state, op, 'seed')
        self.assertEqual([j[0] for j in state['completed']], ['first'])
        self.assertEqual(state['logical_time'], 5)
        self.assertEqual(state['next_sequence'], 2)
        self.assertEqual(state['operation_cursor'], 4)  # rejected operations are side-effect free
        self.assertTrue(run_case(LabCase('scheduler', 'seed', ops, 3)).passed)

    def test_randomized_reference_candidate_agreement_includes_rejected_operations(self):
        rng = random.Random(20261022)
        for system in SUBJECTS:
            for _ in range(30):
                ops = []
                for i in range(20):
                    key = 'k' + str(rng.randrange(4))
                    if system == 'pipeline':
                        op = ({'op': 'put', 'key': key, 'value': str(rng.randrange(10))} if rng.randrange(2) else
                              {'op': 'transform', 'key': key, 'mode': rng.choice(('upper', 'reverse')), 'output': 'out'})
                    else:
                        op = rng.choice(({'op': 'schedule', 'key': key, 'delay': rng.randrange(4)},
                                         {'op': 'cancel', 'key': key}, {'op': 'advance', 'ticks': rng.randrange(4)}))
                    ops.append(op)
                result = run_case(LabCase(system, 'seed', tuple(ops), rng.randrange(21)))
                self.assertTrue(result.passed, result.to_dict())

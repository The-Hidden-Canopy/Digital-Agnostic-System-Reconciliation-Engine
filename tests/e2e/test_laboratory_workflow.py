"""The same three-way/reduction semantics are available through CLI and bounded HTTP APIs."""
import tempfile
from pathlib import Path

from tests.e2e.test_cli import cli
from tests.e2e.test_server import ServerCase
from tests.helpers import WorkspaceCase


class LaboratoryCli(WorkspaceCase):
    def test_both_systems_detect_reduce_and_reverify_failures(self):
        for system, defect in (('pipeline', 'stale_cache'), ('scheduler', 'reverse_ties')):
            with self.subTest(system=system):
                ws = str(self.tmp / system)
                code, result, err = cli('experiment', system, '--ops', '12', ws=ws)
                self.assertEqual(code, 0, err)
                self.assertTrue(result['passed'])
                code, result, err = cli('experiment', system, '--ops', '12', '--defect', defect, ws=ws)
                self.assertEqual(code, 1, err)
                self.assertTrue(result['divergences'])
                target = str(self.tmp / (system + '.json'))
                args = ('minimize', '--system', system, '--defect', defect, '--ops', '12',
                        '--max-oracle-calls', '200', '--output', target)
                code, result, err = cli(*args, ws=ws)
                self.assertEqual(code, 0, err)
                self.assertEqual(result['fresh_process_verification'], 'VERIFIED')
                code, verified, err = cli('verify-counterexample', target, ws=ws)
                self.assertEqual((code, verified['status']), (0, 'VERIFIED'), err)
                self.assertEqual(cli(*args, ws=ws)[0], 2)
                self.assertEqual(cli('experiment', system, '--ops', '401', ws=ws)[0], 2)
                self.assertEqual(cli('experiment', system, '--subject-cmd', 'echo no', ws=ws)[0], 2)


class LaboratoryHttp(ServerCase):
    def test_reference_and_defective_subjects_and_fresh_process_reduction(self):
        for system, defect in (('pipeline', 'stale_cache'), ('scheduler', 'reverse_ties')):
            status, result, _ = self.call('POST', '/api/v1/experiments/' + system, {'ops': 12})
            self.assertEqual(status, 200)
            self.assertTrue(result['passed'])
            status, result, _ = self.call('POST', '/api/v1/experiments/' + system, {'ops': 12, 'defects': [defect]})
            self.assertEqual(status, 200)
            self.assertFalse(result['passed'])
            self.assertTrue(result['divergences'])
            status, result, _ = self.call('POST', '/api/v1/experiments/laboratory-minimize',
                {'system': system, 'ops': 12, 'defects': [defect], 'max_oracle_calls': 200})
            self.assertEqual(status, 200, result)
            self.assertEqual(result['fresh_process_verification'], 'VERIFIED')
            self.assertLess(result['reduced_units'], result['original_units'])
            self.assertEqual(result['package']['schema'], 'verifier-lab/laboratory-counterexample/1')

    def test_http_cannot_select_programs_and_limits_are_not_silently_coerced(self):
        for fields in ({'ops': True}, {'ops': 401}, {'boundary': '3'}, {'defects': 'stale_cache'},
                       {'defects': ['unknown']}, {'subject_cmd': 'echo never'}, {'seed': []}):
            with self.subTest(fields=fields):
                self.assertEqual(self.call('POST', '/api/v1/experiments/pipeline', fields)[0], 422)
        for fields in ({'system': '../evil'}, {'system': 'pipeline', 'max_oracle_calls': 0},
                       {'system': 'scheduler', 'max_oracle_calls': True}):
            self.assertEqual(self.call('POST', '/api/v1/experiments/laboratory-minimize', fields)[0], 422)

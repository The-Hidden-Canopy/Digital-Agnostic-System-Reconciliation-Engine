"""Pure decoding checks: no process profiles, ACL changes or repositories."""
import unittest

from tests.unit.windows_appcontainer_lab import synthetic_worker_failure


class SyntheticProbeDiagnosticTests(unittest.TestCase):
    def test_reply_open_errors_are_distinct_from_other_checks(self):
        self.assertIn("Win32 error 5", synthetic_worker_failure(0x19000005))
        self.assertIn("Win32 error 32", synthetic_worker_failure(0x19000020))
        self.assertIn("Win32 error 65535", synthetic_worker_failure(0x1900ffff))
        self.assertIn("exceeds diagnostic range", synthetic_worker_failure(0x19010000))
        self.assertEqual(synthetic_worker_failure(19), "Synthetic AppContainer probe failed at check 19")
        self.assertEqual(synthetic_worker_failure(0x19010001), "Synthetic AppContainer probe failed at check 419495937")

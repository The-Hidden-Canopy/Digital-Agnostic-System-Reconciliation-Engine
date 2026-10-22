"""Release scans must reject extra files and incomplete distributions."""
import tempfile
import unittest
import zipfile
from pathlib import Path

from tools.check_release import DATA_FILES, METADATA, scan_wheel


class WheelAllowlist(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.wheel = Path(self.tmp.name) / 'test.whl'
        self.modules = {'verifier_lab/__init__.py', 'verifier_lab/cli.py'}
        self.names = self.modules | DATA_FILES | {f'verifier_lab-0.1.0.dist-info/{n}' for n in METADATA}

    def write(self, names, payload=b'fixture'):
        with zipfile.ZipFile(self.wheel, 'w') as archive:
            for name in sorted(names):
                archive.writestr(name, payload)

    def test_exact_distribution_passes(self):
        self.write(self.names)
        self.assertEqual(scan_wheel(self.wheel, self.modules), sorted(self.names))

    def test_extra_and_missing_members_fail(self):
        for names in (self.names | {'verifier_lab/debug.log'}, self.names - {'verifier_lab/web/app.js'},
                      self.names | {'verifier_lab/.env'}, self.names | {'../outside'}):
            with self.subTest(names=names), self.assertRaises(ValueError):
                self.write(names)
                scan_wheel(self.wheel, self.modules)

    def test_duplicate_entries_fail(self):
        self.write(self.names)
        with zipfile.ZipFile(self.wheel, 'a') as archive:
            archive.writestr('verifier_lab/cli.py', b'duplicate')
        with self.assertRaises(ValueError):
            scan_wheel(self.wheel, self.modules)

    def test_local_paths_and_key_material_fail(self):
        for payload in (b'/workspace/private', b'/home/agent/private', b'-----BEGIN PRIVATE KEY-----'):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                self.write(self.names, payload)
                scan_wheel(self.wheel, self.modules)

    def test_symlink_fails(self):
        self.write(self.names - {'verifier_lab/cli.py'})
        with zipfile.ZipFile(self.wheel, 'a') as archive:
            info = zipfile.ZipInfo('verifier_lab/cli.py')
            info.create_system = 3
            info.external_attr = 0o120777 << 16
            archive.writestr(info, '/etc/passwd')
        with self.assertRaises(ValueError):
            scan_wheel(self.wheel, self.modules)

import subprocess
import sys
import unittest

from tests.helpers import ROOT


class Architecture(unittest.TestCase):
    def test_importing_the_core_has_no_network_process_or_plugin_side_effects(self):
        code = ("import sys; sys.path.insert(0, %r);"
                "import verifier_lab.canonical, verifier_lab.model, verifier_lab.validation, verifier_lab.rules, verifier_lab.engine;"
                "bad = [m for m in ('socket', 'subprocess', 'http', 'urllib.request', 'importlib.metadata', 'multiprocessing') if m in sys.modules];"
                "print(bad)" % str(ROOT / "src"))
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
        self.assertEqual(out.strip(), "[]")

    def test_rules_do_not_read_clocks_or_randomness(self):
        for mod in ("rules.py", "engine.py", "canonical.py", "model.py"):
            src = (ROOT / "src/verifier_lab" / mod).read_text()
            for banned in ("import time", "import datetime", "import random", "import uuid", "os.environ", "open(", "import socket", "import subprocess"):
                self.assertNotIn(banned, src, f"{mod} uses {banned}")

    def test_source_tree_has_no_private_or_vendor_references(self):
        banned = ("hidden canopy", "glyph", "strata", "customer", "internal schema")
        for p in (ROOT / "src").rglob("*.py"):
            low = p.read_text().lower()
            for word in banned:
                self.assertNotIn(word, low, f"{p.name}: {word}")


if __name__ == "__main__":
    unittest.main()

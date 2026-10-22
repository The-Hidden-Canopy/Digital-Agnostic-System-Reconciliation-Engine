import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.helpers import ROOT, TUTORIAL


def cli(*args, ws):
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    p = subprocess.run([sys.executable, "-m", "verifier_lab.cli", "--workspace", ws, *args], capture_output=True, text=True, env=env)
    return p.returncode, (json.loads(p.stdout) if p.stdout.strip().startswith(("{", "[")) else p.stdout), p.stderr


class Cli(unittest.TestCase):
    def test_import_approve_check_export_verify_and_exit_codes(self):
        with tempfile.TemporaryDirectory() as ws:
            rc, out, _ = cli("import", str(TUTORIAL / "product-spec.md"), str(TUTORIAL / "service-config.json"), ws=ws)
            self.assertEqual(rc, 0, out)
            spec, cfg = (o["source"]["id"] for o in out)
            rc, cands, _ = cli("candidates", "--source", spec, ws=ws)
            keep = [c["id"] for c in cands["items"] if c["fact_key"] in ("limits.retries", "limits.max_upload_kb")]
            rc, out, _ = cli("approve", *keep, ws=ws)
            self.assertEqual(rc, 0)
            rc, run, _ = cli("check", "--evidence", cfg, ws=ws)
            self.assertEqual(rc, 1)                                   # completed, but a finding needs attention
            self.assertEqual(sorted(f["verdict"] for f in run["findings"]), ["CONTRADICTED", "SUPPORTED"])
            target = str(Path(ws) / "out.zip")
            rc, _, _ = cli("export", run["id"], "--output", target, ws=ws)
            self.assertEqual(rc, 0)
            rc, _, err = cli("export", run["id"], "--output", target, ws=ws)
            self.assertEqual(rc, 2)                                   # refuses to overwrite
            self.assertIn("refusing to overwrite", err)
            rc, res, _ = cli("verify-package", target, ws=ws)
            self.assertEqual((rc, res["status"]), (0, "VERIFIED"))
            data = bytearray(Path(target).read_bytes())
            data[len(data) // 2] ^= 0xFF
            Path(target + ".bad").write_bytes(bytes(data))
            rc, res, _ = cli("verify-package", target + ".bad", ws=ws)
            self.assertEqual((rc, res["status"]), (3, "REJECTED"))

    def test_validate_and_serve_guard(self):
        with tempfile.TemporaryDirectory() as ws:
            bad = Path(ws) / "b.json"
            bad.write_text('{"schema_version": "1"}')
            rc, out, _ = cli("validate", str(bad), ws=ws)
            self.assertEqual((rc, out["valid"]), (2, False))
            rc, _, err = cli("serve", "--host", "0.0.0.0", ws=ws)
            self.assertEqual(rc, 2)
            self.assertIn("loopback", err)

    def test_check_with_unreviewed_candidate_is_refused(self):
        with tempfile.TemporaryDirectory() as ws:
            rc, out, _ = cli("import", str(TUTORIAL / "service-config.json"), ws=ws)
            rc, run, err = cli("check", "--evidence", out[0]["source"]["id"], ws=ws)
            self.assertEqual(rc, 2)        # nothing approved: unreviewed machine suggestions never run as claims


if __name__ == "__main__":
    unittest.main()

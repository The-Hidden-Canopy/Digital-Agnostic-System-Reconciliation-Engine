"""Exercise process interruption with synthetic repositories and an isolated workspace."""
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from verifier_lab.elucidary_desktop import DesktopApp, DesktopController
from verifier_lab.ingestion.common import Limits
from verifier_lab.service import Service


def interrupted_worker(repo, workspace, ready):
    import tkinter as tk
    from verifier_lab import repository_map
    scan = repository_map.scan_repository

    def pause_before_scan_is_published(*args, **kwargs):
        result = scan(*args, **kwargs)
        Path(ready).write_text("scan returned; publication paused", encoding="utf-8")
        while True:
            time.sleep(.05)

    repository_map.scan_repository = pause_before_scan_is_published
    service = Service(Path(workspace), limits=Limits(isolation="portable"))
    window = tk.Tk(); window.withdraw()
    try:
        app = DesktopApp(window, DesktopController(service))
        app.audience.set("Adult"); app.path.set(repo)
        app.isolation.set("Trusted repositories only: no parser sandbox")
        with patch.object(socket, "socket", side_effect=AssertionError("unexpected network")):
            app.open_repository()
            window.mainloop()
    finally:
        service.close(); window.destroy()


class ProcessInterruptionTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "win32" and os.environ.get("ELUCIDARY_TEST_LAUNCHER"),
                         "requires the packaged Windows launcher")
    def test_interrupted_scan_reopens_with_labels_and_launcher(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); repo = root / "repo"; repo.mkdir()
            workspace, ready, marker = root / "workspace", root / "ready", root / "executed"
            (repo / "sample.py").write_text(
                f"from pathlib import Path\nPath({str(marker)!r}).touch()\n"
                "def helper(value): return value\ndef start(): return helper(1)\n", encoding="utf-8")
            with patch.object(socket, "socket", side_effect=AssertionError("unexpected network")):
                service = Service(workspace, limits=Limits(isolation="portable"))
                try:
                    controller = DesktopController(service)
                    controller.open(str(repo), "adult", "portable")
                    controller.select("function:sample.py:helper"); controller.label("Saved helper")
                finally:
                    service.close()
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            worker = subprocess.Popen([sys.executable, __file__, "--worker", str(repo), str(workspace), str(ready)],
                                      creationflags=flags)
            try:
                deadline = time.monotonic() + 30
                while not ready.exists() and worker.poll() is None and time.monotonic() < deadline:
                    time.sleep(.05)
                self.assertTrue(ready.exists(), f"worker did not reach the pause: {worker.poll()}")
                self.assertIsNone(worker.poll(), "worker exited before interruption")
                worker.kill()
                self.assertNotEqual(worker.wait(timeout=10), 0)
            finally:
                if worker.poll() is None:
                    worker.kill(); worker.wait(timeout=10)
            launcher = os.environ["ELUCIDARY_TEST_LAUNCHER"]
            result = subprocess.run([launcher, "--self-check", "--workspace", str(workspace)],
                                    timeout=30, creationflags=flags, check=False)
            self.assertEqual(result.returncode, 0, "packaged launcher could not reopen the workspace")
            with patch.object(socket, "socket", side_effect=AssertionError("unexpected network")):
                service = Service(workspace, limits=Limits(isolation="portable"))
                try:
                    controller = DesktopController(service)
                    scan = controller.open(str(repo), "adult", "portable")
                    self.assertEqual(controller.nodes["function:sample.py:helper"]["label"], "Saved helper")
                    callers = service.repository_possible_callers(scan["repo_id"], "function:sample.py:helper", "beginner", 1, scan["scan_id"])
                    self.assertTrue(any(row["caller_id"] == "function:sample.py:start" for row in callers["rows"]))
                    self.assertEqual(service.store.q1("PRAGMA integrity_check")[0], "ok")
                    self.assertFalse(marker.exists(), "repository source was executed")
                finally:
                    service.close()


if __name__ == "__main__" and len(sys.argv) == 5 and sys.argv[1] == "--worker":
    interrupted_worker(*sys.argv[2:])

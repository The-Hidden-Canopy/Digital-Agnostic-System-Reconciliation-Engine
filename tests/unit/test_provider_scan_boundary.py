"""Synthetic provider/scan acceptance; no model or server is started."""
import hashlib
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path

from verifier_lab.elucidary_desktop import DesktopController
from verifier_lab.ingestion.common import Limits
from verifier_lab.service import Service, ServiceError


class SyntheticProvider:
    def __init__(self, blocked=False):
        self.entered = threading.Event()
        self.release = threading.Event()
        if not blocked:
            self.release.set()
        self.calls = 0

    def safe_for(self, path):
        return True

    def available(self):
        return True

    def generate(self, cards, topic, level):
        self.calls += 1
        self.entered.set()
        if not self.release.wait(15):
            raise RuntimeError("synthetic provider timed out")
        return {"text": "Synthetic interpretation [1].",
                "citations": [{"id": 1, **cards[0]["citation"]}]}


class ProviderScanBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "repo"
        self.root.mkdir()
        self.path = self.root / "m.py"
        self.write_source(1)

    def write_source(self, amount):
        self.path.write_text(
            f"def helper(value):\n    return value + {amount}\n\n"
            f"def start(value):\n    return helper(value + {amount})\n", encoding="utf-8")

    def digest(self):
        return hashlib.sha256(self.path.read_bytes()).hexdigest()

    def assert_stale(self, callback):
        with self.assertRaises(ServiceError) as error:
            callback()
        self.assertEqual(error.exception.code, "stale_repository")

    def test_rescan_during_inflight_provider_discards_old_result_and_recovers(self):
        provider = SyntheticProvider(blocked=True)
        with closing(Service(Path(self.temp.name) / "workspace", limits=Limits(isolation="portable"), local_assistant=provider)) as service:
            controller = DesktopController(service)
            old = controller.open(str(self.root), "adult", "portable")
            repo, sid = old["repo_id"], old["scan_id"]
            old_digest = self.digest()
            result = []

            def request():
                try:
                    result.append(service.repository_explain(repo, "function:m.py:start", "calls", "beginner", sid, True))
                except Exception as exc:
                    result.append(exc)

            worker = threading.Thread(target=request, daemon=True)
            worker.start()
            try:
                self.assertTrue(provider.entered.wait(10), "synthetic provider did not start")
                self.write_source(2)
                current = controller.open(str(self.root), "adult", "portable", baseline=(old, "adult"))
                self.assertNotEqual(sid, current["scan_id"])
                self.assertNotEqual(old_digest, self.digest())
                self.assertGreater(current["comparison"]["counts"]["changed"], 0)
            finally:
                provider.release.set()
                worker.join(15)
            self.assertFalse(worker.is_alive())
            self.assertEqual(len(result), 1)
            self.assertIsInstance(result[0], ServiceError)
            self.assertEqual(result[0].code, "stale_repository")
            self.assert_stale(lambda: service.repository_compare_call(repo, "function:m.py:start", 0, "beginner", sid))
            self.assert_stale(lambda: service.repository_practice(repo, "function:m.py:start", "source", sid))
            self.assert_stale(lambda: service.repository_reading_questions(repo, "function:m.py:helper", "beginner", sid))
            controller.select("function:m.py:start")
            compared = controller.compare(0, "beginner")
            self.assertEqual(compared["call"]["sha256"], self.digest())
            fresh = controller.explain("calls", "beginner", True)
            self.assertEqual(fresh["assistant"]["status"], "ready")
            self.assertEqual(fresh["assistant"]["citations"][0]["sha256"], self.digest())
            self.assertTrue(service.repository_practice(repo, controller.selected, "source", current["scan_id"])["correct"])
            questions = service.repository_reading_questions(repo, "function:m.py:helper", "beginner", current["scan_id"])
            self.assertEqual(questions[0]["evidence"]["sha256"], self.digest())

    def test_provider_replacement_preserves_practice_and_requires_new_scan(self):
        first, replacement = SyntheticProvider(), SyntheticProvider()
        with closing(Service(Path(self.temp.name) / "workspace", limits=Limits(isolation="portable"), local_assistant=first)) as service:
            controller = DesktopController(service)
            initial = controller.open(str(self.root), "adult", "portable")
            repo, sid = initial["repo_id"], initial["scan_id"]
            controller.select("function:m.py:helper")
            controller.label("My helper")
            service.repository_progress(repo, controller.selected, True, sid)
            old = controller.explain("returns", "beginner", True)
            old_citation = old["assistant"]["citations"][0]
            self.assertEqual(old_citation["sha256"], self.digest())
            questions = service.repository_reading_questions(repo, controller.selected, "beginner", sid)
            self.assertTrue(questions)
            service.local_assistant = replacement
            self.assertEqual(controller.explain("returns", "beginner", True)["assistant"]["status"], "unavailable")
            controller.select("function:m.py:start")
            self.assertTrue(controller.compare(0, "beginner"))
            self.assertTrue(service.repository_practice(repo, controller.selected, "source", sid)["correct"])
            self.assertEqual(replacement.calls, 0)
            self.write_source(3)
            current = controller.open(str(self.root), "adult", "portable", baseline=(initial, "adult"))
            self.assert_stale(lambda: service.repository_check_reading(repo, "function:m.py:helper", "beginner", sid, questions[0]["kind"], 0))
            controller.select("function:m.py:helper")
            self.assertEqual(controller.nodes[controller.selected]["label"], "My helper")
            self.assertTrue(controller.nodes[controller.selected]["review_needs_refresh"])
            new = controller.explain("returns", "beginner", True)
            self.assertEqual(new["assistant"]["status"], "ready")
            self.assertEqual(new["assistant"]["citations"][0]["sha256"], self.digest())
            self.assertNotEqual(old_citation["sha256"], self.digest())
            self.assertNotEqual(current["scan_id"], sid)
            self.assertEqual(replacement.calls, 1)
            fresh_questions = service.repository_reading_questions(repo, controller.selected, "beginner", current["scan_id"])
            self.assertEqual(fresh_questions[0]["evidence"]["sha256"], self.digest())
            checked = service.repository_check_reading(repo, controller.selected, "beginner", current["scan_id"], fresh_questions[0]["kind"], 0)
            self.assertEqual(checked["evidence"]["sha256"], self.digest())
            controller.select("function:m.py:start")
            self.assertEqual(controller.compare(0, "beginner")["call"]["sha256"], self.digest())
            self.assertTrue(service.repository_practice(repo, controller.selected, "source", current["scan_id"])["correct"])
            self.assertEqual(service.store.q1("SELECT COUNT(*) AS n FROM repository_progress")["n"], 1)

    def test_provider_swap_during_inflight_result_falls_back_and_recovers(self):
        first, replacement = SyntheticProvider(blocked=True), SyntheticProvider()
        with closing(Service(Path(self.temp.name) / "workspace", limits=Limits(isolation="portable"), local_assistant=first)) as service:
            controller = DesktopController(service)
            old = controller.open(str(self.root), "adult", "portable")
            controller.select("function:m.py:start")
            result = []

            def request():
                try:
                    result.append(service.repository_explain(old["repo_id"], "function:m.py:start", "calls", "beginner", old["scan_id"], True))
                except Exception as exc:
                    result.append(exc)

            worker = threading.Thread(target=request, daemon=True)
            worker.start()
            try:
                self.assertTrue(first.entered.wait(10), "synthetic provider did not start")
                service.local_assistant = replacement
                self.assertTrue(controller.compare(0, "beginner"))
                self.assertTrue(service.repository_practice(old["repo_id"], controller.selected, "source", old["scan_id"])["correct"])
            finally:
                first.release.set()
                worker.join(15)
            self.assertFalse(worker.is_alive())
            self.assertEqual(len(result), 1)
            self.assertIsInstance(result[0], dict)
            self.assertEqual(result[0]["assistant"], {"status": "unavailable"})
            self.assertTrue(result[0]["facts"])
            self.assertEqual(replacement.calls, 0)
            self.assertEqual(controller.explain("calls", "beginner", True)["assistant"]["status"], "unavailable")
            current = controller.open(str(self.root), "adult", "portable", baseline=(old, "adult"))
            controller.select("function:m.py:start")
            ready = controller.explain("calls", "beginner", True)["assistant"]
            self.assertEqual(ready["status"], "ready")
            self.assertEqual(ready["citations"][0]["sha256"], self.digest())
            self.assertNotEqual(current["scan_id"], old["scan_id"])


if __name__ == "__main__":
    unittest.main()

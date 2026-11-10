import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from verifier_lab.repository_assistant import LocalAssistant, prepare_prompt, validate_answer


class _Input:
    def __init__(self): self.value = b""
    def write(self, data): self.value += data
    def close(self): pass


class _Process:
    def __init__(self, output):
        self.stdin, self.stdout, self.returncode = _Input(), io.BytesIO(output), 0
    def wait(self, timeout=None): return self.returncode
    def poll(self): return self.returncode
    def kill(self): self.returncode = -1


class LocalAssistantTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.engine, self.model = root / "engine", root / "model.gguf"
        self.engine.write_text("engine"); self.model.write_text("model")
        self.assistant = LocalAssistant(self.engine, self.model)
        self.evidence = [{"citation": {"path": "m.py", "line": 1, "sha256": "abc", "text": "return x", "evidence": "static"}}]

    def generate(self, output):
        proc = _Process(output)
        with patch.object(self.assistant, "available", return_value=True), \
             patch("verifier_lab.repository_assistant.shutil.which", return_value="/usr/bin/bwrap"), \
             patch("verifier_lab.repository_assistant.subprocess.Popen", return_value=proc) as run:
            result = self.assistant.generate(self.evidence, "returns", "beginner")
        return result, proc, run.call_args

    def test_completion_is_isolated_and_citations_are_mapped_to_source(self):
        result, proc, call = self.generate(b"The return expression is x [1].")
        argv = call.args[0]
        self.assertIn("--unshare-all", argv)
        self.assertIn("--clearenv", argv)
        self.assertIn("--ro-bind", argv)
        self.assertNotIn("return x", " ".join(argv))
        self.assertIn(b"return x", proc.stdin.value)
        self.assertEqual(result["citations"][0]["path"], "m.py")
        self.assertEqual(result["citations"][0]["line"], 1)

    def test_missing_or_invalid_citation_is_rejected(self):
        for output in (b"An unreferenced answer.", b"Invalid [2].", b"Unknown [0]."):
            with self.subTest(output=output), self.assertRaises(ValueError):
                self.generate(output)

    def test_configured_engine_and_model_must_be_outside_repository(self):
        self.assertFalse(self.assistant.safe_for(self.engine.parent))
        unrelated = self.engine.parent / "repo"; unrelated.mkdir()
        self.assertTrue(self.assistant.safe_for(unrelated))

    def test_unavailable_sandbox_does_not_start_engine(self):
        with patch.object(self.assistant, "available", return_value=False), \
             patch("verifier_lab.repository_assistant.subprocess.Popen") as run:
            with self.assertRaises(RuntimeError):
                self.assistant.generate(self.evidence, "returns", "beginner")
            run.assert_not_called()

    def test_transport_independent_prompt_and_citation_validation(self):
        evidence = [{"citation": {"path": f"{i}.py", "line": i + 1, "text": "source " + str(i), "evidence": "static"}} for i in range(5)]
        prompt, count = prepare_prompt(evidence, "calls", "explore")
        self.assertEqual(count, 4); self.assertIn("source 3", prompt); self.assertNotIn("source 4", prompt)
        result = validate_answer("Possible relationship [2].", evidence, count)
        self.assertEqual((result["citations"][0]["id"], result["citations"][0]["path"]), (2, "1.py"))
        for answer in ("No reference", "Out of range [5]", "x" * 4001 + "[1]"):
            with self.subTest(answer=answer[:25]), self.assertRaises(ValueError): validate_answer(answer, evidence, count)

    def test_invalid_learning_context_does_not_start_model(self):
        with patch("verifier_lab.repository_assistant.subprocess.Popen") as start:
            with self.assertRaises(ValueError): self.assistant.generate(self.evidence, "unexpected", "beginner")
            with self.assertRaises(ValueError): self.assistant.generate(self.evidence, "returns", "unknown")
            start.assert_not_called()

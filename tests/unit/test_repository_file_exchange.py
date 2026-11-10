import socket
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from verifier_lab.ingestion.common import Limits
from verifier_lab.ingestion.extract import ExtractionFailure, extract_isolated
from verifier_lab.ingestion.file_exchange import _decode_reply, experimental_file_exchange, worker_main


class FileExchangeTests(unittest.TestCase):
    def test_untrusted_reply_rejects_ambiguous_and_invalid_envelopes(self):
        bad = [b'{"ok":true,"ok":false,"extraction":{}}',
               b'{"ok":true,"extraction":{"x":1,"x":2}}',
               b'{"ok":true,"extraction":{"x":NaN}}',
               b'{"ok":true,"extraction":{"x":1e999}}',
               b'{"ok":true,"extraction":{},"error":"memory_limit"}',
               b'{"ok":false,"error":"arbitrary attacker text"}',
               b'{"ok":false,"error":[]}', b'{"ok":1,"extraction":{}}',
               '{"ok":true,"extraction":{}}'.encode("utf-16"),
               b'{"ok":true,"extraction":' + b'[' * 1100 + b'0' + b']' * 1100 + b'}']
        for payload in bad:
            with self.subTest(payload=payload[:80]), self.assertRaises(ExtractionFailure) as error:
                _decode_reply(payload)
            self.assertEqual(error.exception.code, "worker_protocol_error")
        self.assertEqual(_decode_reply(b'{"ok":true,"extraction":{"nodes":[]}}'), {"nodes": []})
        with self.assertRaises(ExtractionFailure) as error:
            _decode_reply(b'{"ok":false,"error":"memory_limit"}')
        self.assertEqual(error.exception.code, "memory_limit")

    def test_synthetic_round_trip_matches_pipe_transport_without_inherited_pipe_arguments(self):
        samples = (("repository-python", b"def f(x): return x\n", "example.py"),
                   ("repository-javascript", b"function f(x) { return x; }\n", "example.js"),
                   ("repository-typescript", b"function f(x: number): number { return x; }\n", "example.ts"),
                   ("repository-cpp", b"int f(int x) { return x; }\n", "example.cpp"))
        limits = Limits(isolation="portable")
        with patch.object(socket, "socket", side_effect=AssertionError("network is not part of this protocol")):
            for fmt, data, name in samples:
                with self.subTest(fmt=fmt):
                    expected = extract_isolated(fmt, data, name, limits)
                    with patch("verifier_lab.ingestion.file_exchange.subprocess.Popen", wraps=subprocess.Popen) as start:
                        actual = experimental_file_exchange(fmt, data, name, limits)
                    self.assertEqual(actual, expected)
                    kwargs = start.call_args.kwargs
                    self.assertTrue(kwargs["close_fds"])
                    self.assertNotIn("stdin", kwargs); self.assertNotIn("stdout", kwargs); self.assertNotIn("stderr", kwargs)

    def test_untrusted_source_is_parsed_but_never_executed(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "executed"
            data = f"from pathlib import Path\nPath({str(marker)!r}).touch()\ndef f(): return 1\n".encode()
            result = experimental_file_exchange("repository-python", data, "example.py", Limits(isolation="portable"))
            self.assertTrue(any(n["name"] == "f" for n in result["nodes"]))
            self.assertFalse(marker.exists())

    def test_required_mode_and_oversize_input_fail_before_starting_a_process(self):
        with patch("verifier_lab.ingestion.file_exchange.subprocess.Popen") as start:
            with self.assertRaises(ExtractionFailure) as missing:
                experimental_file_exchange("repository-python", b"pass", "x.py", Limits(isolation="required"))
            self.assertEqual(missing.exception.code, "isolation_unavailable")
            with self.assertRaises(ExtractionFailure) as size:
                experimental_file_exchange("repository-python", b"12345", "x.py", Limits(isolation="portable", max_bytes=4))
            self.assertEqual(size.exception.code, "bad_exchange_request")
            start.assert_not_called()

    def test_worker_rejects_required_mode_before_opening_exchange_files(self):
        import json
        request = json.dumps({"format": "repository-python", "dialect": "x.py", "limits": Limits(isolation="required").to_dict()})
        with patch("pathlib.Path.open", side_effect=AssertionError("must not open files")):
            self.assertEqual(worker_main(["unused", request, "input", "output"]), 2)

    def test_output_is_bounded_and_bad_worker_output_is_rejected(self):
        with patch("verifier_lab.ingestion.file_exchange.MAX_OUTPUT", 20):
            with self.assertRaises(ExtractionFailure) as error:
                experimental_file_exchange("repository-python", b"def f(): pass\n", "x.py", Limits(isolation="portable"))
            self.assertEqual(error.exception.code, "output_limit")
        class FakeProcess:
            returncode = 0
            def wait(self, timeout=None): return 0
        def malformed(argv, **kwargs):
            Path(argv[-1]).write_bytes(b"not json")
            return FakeProcess()
        with patch("verifier_lab.ingestion.file_exchange.subprocess.Popen", side_effect=malformed):
            with self.assertRaises(ExtractionFailure) as error:
                experimental_file_exchange("repository-python", b"pass\n", "x.py", Limits(isolation="portable"))
            self.assertEqual(error.exception.code, "worker_protocol_error")

    def test_timeout_kills_worker_and_reports_failure(self):
        class StalledProcess:
            returncode = None
            killed = False
            def wait(self, timeout=None):
                if not self.killed: raise subprocess.TimeoutExpired("worker", timeout)
                return -1
            def kill(self): self.killed = True; self.returncode = -1
        proc = StalledProcess()
        with patch("verifier_lab.ingestion.file_exchange.subprocess.Popen", return_value=proc):
            with self.assertRaises(ExtractionFailure) as error:
                experimental_file_exchange("repository-python", b"pass\n", "x.py", Limits(isolation="portable", wall_seconds=1))
            self.assertEqual(error.exception.code, "wall_clock_timeout"); self.assertTrue(proc.killed)


if __name__ == "__main__":
    unittest.main()

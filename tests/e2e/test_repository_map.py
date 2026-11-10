import json
from contextlib import closing
import tempfile
import threading
import unittest
from pathlib import Path

from verifier_lab.repository_map import scan_repository
from verifier_lab.ingestion.common import Limits
from verifier_lab.ingestion.extract import ExtractionFailure
from verifier_lab.server import Workbench
from verifier_lab.service import Service, ServiceError
from tests.e2e.test_server import ServerCase


class RepositoryMapTests(unittest.TestCase):
    def test_compare_call_with_inferred_cross_file_target_without_claiming_binding(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"; root.mkdir()
            (root / "lib.py").write_text("def f(a, *, b=3):\n    return a + b\n", encoding="utf-8")
            (root / "main.py").write_text("from lib import f\ndef start(x):\n    return f(x, b=2) + x.method()\n", encoding="utf-8")
            svc = Service(Path(tmp) / "ws", limits=Limits(isolation="portable"))
            try:
                scan = svc.repository_map(str(root)); repo, sid = scan["repo_id"], scan["scan_id"]
                result = svc.repository_compare_call(repo, "function:main.py:start", 0, "beginner", sid)
                self.assertEqual((result["call"]["path"], result["call"]["line"], result["call"]["written_arguments"]), ("main.py", 3, 2))
                self.assertEqual((result["target"]["path"], result["target"]["line"]), ("lib.py", 1))
                self.assertEqual([p["name"] for p in result["target"]["parameters"]], ["a", "b"])
                self.assertEqual(result["relationship"]["evidence"], "inferred")
                self.assertIn("not a mapping", result["limitation"])
                self.assertIn("not an observed call", result["limitation"])
                self.assertTrue(result["call"]["sha256"] and result["target"]["sha256"])
                with self.assertRaises(ServiceError) as unresolved:
                    svc.repository_compare_call(repo, "function:main.py:start", 1, "beginner", sid)
                self.assertEqual(unresolved.exception.code, "target_unresolved")
                with self.assertRaises(ServiceError): svc.repository_compare_call(repo, "function:main.py:start", True, "beginner", sid)
                svc.repository_map(str(root))
                with self.assertRaises(ServiceError) as stale: svc.repository_compare_call(repo, "function:main.py:start", 0, "beginner", sid)
                self.assertEqual(stale.exception.code, "stale_repository")
            finally:
                svc.close()

    def test_repository_shelf_is_opt_in_persistent_and_does_not_delete_labels(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"; root.mkdir()
            (root / "m.py").write_text("def f(): return 1\n", encoding="utf-8")
            ws = Path(tmp) / "ws"
            svc = Service(ws, limits=Limits(isolation="portable"))
            try:
                scan = svc.repository_map(str(root), content_access="adult")
                self.assertEqual(svc.repository_shelf()["items"], [])
                svc.repository_label(scan["repo_id"], "function:m.py:f", "Entry", scan["scan_id"])
                svc.repository_remember(scan["repo_id"], scan["scan_id"])
                self.assertEqual(svc.repository_shelf()["items"][0]["path"], str(root.resolve()))
            finally:
                svc.close()
            svc = Service(ws, limits=Limits(isolation="portable"))
            try:
                self.assertEqual(len(svc.repository_shelf()["items"]), 1)
                child = svc.repository_map(str(root), content_access="adult_reviewed")
                with self.assertRaises(ServiceError) as err: svc.repository_remember(child["repo_id"], child["scan_id"])
                self.assertEqual(err.exception.code, "adult_session_required")
                svc.repository_forget(child["location_id"])
                self.assertEqual(svc.repository_shelf()["items"], [])
                self.assertEqual(next(n for n in child["nodes"] if n["id"] == "function:m.py:f")["label"], "Entry")
            finally:
                svc.close()

    def test_file_source_preview_is_bounded_and_matches_the_scan_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"; root.mkdir()
            source = root / "m.py"
            source.write_text("def start():\n    return '<script>'\n#" + "x" * 700 + "\n" + "# line\n" * 130, encoding="utf-8")
            svc = Service(Path(tmp) / "ws", limits=Limits(isolation="portable"))
            try:
                scan = svc.repository_map(str(root))
                file = next(n for n in scan["nodes"] if n["id"] == "file:m.py")
                function = next(n for n in scan["nodes"] if n["id"] == "function:m.py:start")
                self.assertEqual(file["source_preview"][0], {"line": 1, "text": "def start():", "truncated": False})
                self.assertEqual(function["line"], file["source_preview"][0]["line"])
                self.assertIn("<script>", file["source_preview"][1]["text"])
                self.assertTrue(file["source_preview"][2]["truncated"])
                self.assertEqual(len(file["source_preview"]), 120)
                self.assertTrue(file["source_preview_truncated"])
                source.write_text("def changed(): pass\n", encoding="utf-8")
                self.assertEqual(file["source_preview"][0]["text"], "def start():")
                self.assertNotEqual(next(n for n in svc.repository_map(str(root))["nodes"] if n["id"] == "file:m.py")["sha256"], file["sha256"])
            finally:
                svc.close()

    def test_path_to_saved_label_uses_current_store_and_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"; root.mkdir()
            (root / "m.py").write_text("def helper(): return 1\ndef start(): return helper()\n", encoding="utf-8")
            svc = Service(Path(tmp) / "ws", limits=Limits(isolation="portable"))
            try:
                scan = svc.repository_map(str(root)); repo, sid = scan["repo_id"], scan["scan_id"]
                start, target = "function:m.py:start", "function:m.py:helper"
                args = (repo, start, target, "beginner", True, False, sid)
                with self.assertRaises(ServiceError) as missing:
                    svc.repository_label_path(*args)
                self.assertEqual(missing.exception.code, "label_unavailable")
                svc.repository_label(repo, target, "My helper", sid)
                path = svc.repository_label_path(*args)
                self.assertEqual(path["steps"][-1]["node_id"], target)
                via = path["steps"][-1]["via"]
                self.assertEqual((via["kind"], via["evidence"], via["path"], via["line"]), ("possible call", "inferred", "m.py", 2))
                svc.repository_label(repo, target, "", sid)
                with self.assertRaises(ServiceError): svc.repository_label_path(*args)
                next_scan = svc.repository_map(str(root))
                with self.assertRaises(ServiceError) as stale: svc.repository_label_path(*args)
                self.assertEqual(stale.exception.code, "stale_repository")
                self.assertNotEqual(next_scan["scan_id"], sid)
            finally:
                svc.close()

    def test_local_assistant_requires_explicit_adult_request_and_falls_back(self):
        class FakeLocalAssistant:
            def __init__(self): self.calls = 0; self.fail = False
            def available(self): return True
            def safe_for(self, path): return True
            def generate(self, cards, topic, level):
                self.calls += 1
                if self.fail: raise RuntimeError("private details")
                return {"text": "The source defines a function [1].", "citations": [{"id": 1, **cards[0]["citation"]}]}

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"; root.mkdir()
            (root / "m.py").write_text("def f(value):\n    return value\n", encoding="utf-8")
            fake = FakeLocalAssistant()
            svc = Service(Path(tmp) / "ws", limits=Limits(isolation="portable"), local_assistant=fake)
            try:
                scan = svc.repository_map(str(root), content_access="adult")
                args = (scan["repo_id"], "function:m.py:f", "inputs", "beginner", scan["scan_id"])
                self.assertTrue(scan["local_assistant_available"])
                self.assertNotIn("assistant", svc.repository_explain(*args))
                self.assertEqual(fake.calls, 0)
                result = svc.repository_explain(*args, use_local_assistant=True)
                self.assertEqual(result["assistant"]["status"], "ready")
                self.assertEqual(result["assistant"]["citations"][0]["line"], 1)
                self.assertIn("not verified", result["assistant"]["limitation"])
                fake.fail = True
                failed = svc.repository_explain(*args, use_local_assistant=True)
                self.assertEqual(failed["assistant"]["status"], "unavailable")
                self.assertTrue(failed["facts"])
                self.assertNotIn("private details", str(failed))
                child = svc.repository_map(str(root), content_access="adult_reviewed")
                self.assertFalse(child["local_assistant_available"])
                with self.assertRaises(ServiceError) as err:
                    svc.repository_explain(child["repo_id"], args[1], args[2], args[3], child["scan_id"], use_local_assistant=True)
                self.assertEqual(err.exception.code, "assistant_adult_only")
                self.assertEqual(fake.calls, 2)
            finally:
                svc.close()

    def test_local_assistant_permission_is_bound_to_provider_and_rechecked(self):
        class FakeAssistant:
            def __init__(self, safe=True): self.safe = safe; self.calls = 0; self.on_generate = None; self.on_safe = None; self.on_available = None
            def available(self):
                if self.on_available: self.on_available()
                return True
            def safe_for(self, path):
                if self.on_safe: self.on_safe()
                return self.safe
            def generate(self, cards, topic, level):
                self.calls += 1
                if self.on_generate: self.on_generate()
                return {"text": "Synthetic answer [1].", "citations": [{"id": 1, **cards[0]["citation"]}]}

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"; root.mkdir()
            (root / "m.py").write_text("def f(value):\n    return value\n", encoding="utf-8")
            first, replacement = FakeAssistant(), FakeAssistant()
            with closing(Service(Path(tmp) / "ws", limits=Limits(isolation="portable"), local_assistant=first)) as svc:
                scan = svc.repository_map(str(root), content_access="adult")
                args = (scan["repo_id"], "function:m.py:f", "inputs", "beginner", scan["scan_id"])
                first.safe = False
                self.assertEqual(svc.repository_explain(*args, use_local_assistant=True)["assistant"]["status"], "unavailable")
                self.assertEqual(first.calls, 0)
                first.safe = True
                first.on_safe = lambda: setattr(svc, "local_assistant", replacement)
                self.assertEqual(svc.repository_explain(*args, use_local_assistant=True)["assistant"]["status"], "unavailable")
                self.assertEqual(first.calls, 0)
                first.on_safe = None
                svc.local_assistant = replacement
                self.assertEqual(svc.repository_explain(*args, use_local_assistant=True)["assistant"]["status"], "unavailable")
                self.assertEqual(replacement.calls, 0)
                scan = svc.repository_map(str(root), content_access="adult")
                args = (scan["repo_id"], "function:m.py:f", "inputs", "beginner", scan["scan_id"])
                self.assertEqual(svc.repository_explain(*args, use_local_assistant=True)["assistant"]["status"], "ready")
                replacement.on_generate = lambda: setattr(svc, "local_assistant", first)
                self.assertEqual(svc.repository_explain(*args, use_local_assistant=True)["assistant"]["status"], "unavailable")
                self.assertEqual(first.calls, 0)
                # A replacement during the scan must not inherit the original check.
                first.on_safe = lambda: setattr(svc, "local_assistant", replacement)
                replacement.on_generate = None
                scan = svc.repository_map(str(root), content_access="adult")
                self.assertFalse(scan["local_assistant_available"])
                args = (scan["repo_id"], "function:m.py:f", "inputs", "beginner", scan["scan_id"])
                self.assertEqual(svc.repository_explain(*args, use_local_assistant=True)["assistant"]["status"], "unavailable")
                self.assertEqual(replacement.calls, 2)
                first.on_safe = None
                scan = svc.repository_map(str(root), content_access="adult")
                args = (scan["repo_id"], "function:m.py:f", "inputs", "beginner", scan["scan_id"])
                replacement.on_generate = lambda: svc.repository_map(str(root), content_access="adult_reviewed")
                with self.assertRaises(ServiceError) as stale:
                    svc.repository_explain(*args, use_local_assistant=True)
                self.assertEqual(stale.exception.code, "stale_repository")
                svc.local_assistant = first
                first.on_available = lambda: setattr(svc, "local_assistant", replacement)
                scan = svc.repository_map(str(root), content_access="adult")
                self.assertFalse(scan["local_assistant_available"])
                args = (scan["repo_id"], "function:m.py:f", "inputs", "beginner", scan["scan_id"])
                self.assertEqual(svc.repository_explain(*args, use_local_assistant=True)["assistant"]["status"], "unavailable")
                self.assertEqual(first.calls, 0)
                def unavailable(): raise RuntimeError("synthetic private provider detail")
                first.on_available = unavailable
                svc.local_assistant = first
                scan = svc.repository_map(str(root), content_access="adult")
                self.assertFalse(scan["local_assistant_available"])
                args = (scan["repo_id"], "function:m.py:f", "inputs", "beginner", scan["scan_id"])
                fallback = svc.repository_explain(*args, use_local_assistant=True)
                self.assertEqual(fallback["assistant"]["status"], "unavailable")
                self.assertTrue(fallback["facts"])
                self.assertNotIn("synthetic private provider detail", str(fallback))

    def test_read_only_map_evidence_and_persistent_labels(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            root.mkdir()
            marker = Path(tmp) / "executed"
            (root / "main.py").write_text(
                f"from pathlib import Path\nPath({str(marker)!r}).touch()\n"
                "def greet(name):\n    return name\n\n"
                "def start():\n    return greet('world')\n", encoding="utf-8")
            hidden = root / ".private"
            hidden.mkdir()
            (hidden / "secret.py").write_text("def secret(): pass", encoding="utf-8")
            (root / "broken.py").write_text("def broken(:", encoding="utf-8")
            workspace = Path(tmp) / "workspace"
            svc = Service(workspace, limits=Limits(isolation="portable"))
            try:
                result = svc.repository_map(str(root))
                self.assertFalse(marker.exists())
                self.assertEqual(result["files_inspected"], 1)
                self.assertTrue(any("broken.py" in w for w in result["warnings"]))
                self.assertFalse(any("secret" in n["name"] for n in result["nodes"]))
                start = next(n for n in result["nodes"] if n["name"] == "start")
                greet = next(n for n in result["nodes"] if n["name"] == "greet")
                self.assertEqual(start["line"], 6)
                self.assertIn("return greet", start["snippet"])
                self.assertEqual(len(start["sha256"]), 64)
                self.assertTrue(any(e["source"] == start["id"] and e["target"] == greet["id"] and e["evidence"] == "inferred" for e in result["edges"]))
                self.assertTrue(any(e["kind"] == "imports" and e["evidence"] == "static" for e in result["edges"]))
                self.assertFalse(any(e["evidence"] == "observed" for e in result["edges"]))
                svc.repository_label(result["repo_id"], start["id"], "My entry point", result["scan_id"])
                source = root / "main.py"
                source.write_text(source.read_text(encoding="utf-8").replace("return greet('world')", "return greet('friend')"), encoding="utf-8")
                changed = svc.repository_map(str(root))
                self.assertTrue(next(n for n in changed["nodes"] if n["id"] == start["id"])["label_needs_review"])
                with self.assertRaises(ServiceError):
                    svc.repository_label(result["repo_id"], start["id"], "x\ny", changed["scan_id"])
            finally:
                svc.close()
            svc = Service(workspace, limits=Limits(isolation="portable"))
            try:
                reopened = svc.repository_map(str(root))
                self.assertEqual(next(n for n in reopened["nodes"] if n["id"] == start["id"])["label"], "My entry point")
                self.assertFalse(marker.exists())
            finally:
                svc.close()

    def test_real_darse_example(self):
        root = Path(__file__).resolve().parents[2] / "examples"
        result = scan_repository(str(root), limits=Limits(isolation="portable"))
        self.assertGreater(result["files_inspected"], 0)
        self.assertTrue(any(n["kind"] == "function" and n["path"] == "reference_subject.py" for n in result["nodes"]))

    def test_written_calls_include_unresolved_and_never_claim_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"; root.mkdir()
            (root / "m.py").write_text("def target(x): return x\ndef start(obj):\n    return target(1) + obj.run(2) + obj[0](3)\n", encoding="utf-8")
            svc = Service(Path(tmp) / "ws", limits=Limits(isolation="portable"))
            try:
                scan = svc.repository_map(str(root)); ident = "function:m.py:start"
                answer = svc.repository_explain(scan["repo_id"], ident, "calls", "beginner", scan["scan_id"])
                calls = answer["call_sites"]
                self.assertEqual([c["written"] for c in calls], ["target", "obj.run", "obj[0]"])
                self.assertEqual([c["resolution"] for c in calls], ["inferred", "unresolved", "unresolved"])
                self.assertEqual([c["evidence"] for c in calls], ["static", "static", "static"])
                self.assertEqual([c["written_arguments"] for c in calls], [1, 1, 1])
                self.assertEqual(calls[0]["target_id"], "function:m.py:target")
                self.assertIsNone(calls[1]["target_id"])
                self.assertEqual((calls[2]["path"], calls[2]["line"]), ("m.py", 3))
                self.assertTrue(any("do not establish" in f for f in answer["facts"]))
            finally:
                svc.close()

    def test_call_sites_are_selectable_labeled_and_recovered_safely(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"; root.mkdir()
            path = root / "m.py"
            path.write_text("def target(x): return x\ndef start(obj):\n    target(1)\n    obj.work(2)\n", encoding="utf-8")
            svc = Service(Path(tmp) / "ws", limits=Limits(isolation="portable"))
            try:
                scan = svc.repository_map(str(root))
                known = next(n for n in scan["nodes"] if n["kind"] == "callsite" and n["name"] == "target")
                unknown = next(n for n in scan["nodes"] if n["kind"] == "callsite" and n["name"] == "obj.work")
                caller = next(n for n in scan["nodes"] if n["id"] == "function:m.py:start")
                self.assertEqual(next(c for c in caller["call_sites"] if c["written"] == "obj.work")["map_node_id"], unknown["id"])
                self.assertTrue(any(e["source"] == "function:m.py:start" and e["target"] == known["id"] and e["kind"] == "contains written call" and e["evidence"] == "static" for e in scan["edges"]))
                self.assertTrue(any(e["source"] == known["id"] and e["target"] == "function:m.py:target" and e["kind"] == "possible target" and e["evidence"] == "inferred" for e in scan["edges"]))
                self.assertFalse(any(e["source"] == unknown["id"] and e["kind"] == "possible target" for e in scan["edges"]))
                self.assertTrue(svc.repository_practice(scan["repo_id"], unknown["id"], "source", scan["scan_id"], "callsite")["correct"])
                svc.repository_label(scan["repo_id"], unknown["id"], "Uncertain work call", scan["scan_id"])
                path.write_text("def target(x): return x\ndef start(obj):\n    added()\n    target(1)\n    obj.work(2)\n", encoding="utf-8")
                changed = svc.repository_map(str(root))
                item = next(n for n in changed["nodes"] if n["kind"] == "callsite" and n["name"] == "obj.work")
                self.assertNotEqual(item["id"], unknown["id"])
                self.assertEqual(item["label"], "")
                orphan = next(x for x in changed["unmatched_labels"] if x["label"] == "Uncertain work call")
                self.assertEqual(orphan["suggested_node_id"], item["id"])
                svc.repository_rebind_label(changed["repo_id"], unknown["id"], item["id"], changed["scan_id"])
                refreshed = svc.repository_map(str(root))
                self.assertEqual(next(n for n in refreshed["nodes"] if n["id"] == item["id"])["label"], "Uncertain work call")
            finally:
                svc.close()

    def test_call_site_map_limit_keeps_written_calls_accessible(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            text = "".join(f"def f{i}():\n" + "    unknown()\n" * 30 for i in range(6))
            (root / "many.py").write_text(text, encoding="utf-8")
            scan = scan_repository(tmp, limits=Limits(isolation="portable"))
            self.assertEqual(sum(n["kind"] == "callsite" for n in scan["nodes"]), 150)
            self.assertTrue(any("30 recorded call sites have no map node" in w for w in scan["warnings"]))
            self.assertEqual(len(next(n for n in scan["nodes"] if n["id"] == "function:many.py:f5")["call_sites"]), 30)

    def test_written_calls_in_other_languages_remain_unresolved_when_dynamic(self):
        try:
            import esprima, tree_sitter, tree_sitter_typescript, tree_sitter_cpp
        except ImportError:
            self.skipTest("optional parsers are not installed")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "m.js").write_text("export function go(obj) { obj[key](1); }\n", encoding="utf-8")
            (root / "m.ts").write_text("export function go(obj: any) { obj.work(1); }\n", encoding="utf-8")
            (root / "m.cpp").write_text("struct T { void work(int); }; void go(T& obj) { obj.work(1); }\n", encoding="utf-8")
            scan = scan_repository(tmp, limits=Limits(isolation="portable"))
            for ident in ("function:m.js:go", "function:m.ts:go", "function:m.cpp:go"):
                with self.subTest(ident=ident):
                    item = next(n for n in scan["nodes"] if n["id"] == ident)
                    self.assertEqual(item["call_sites"][0]["resolution"], "unresolved")
                    self.assertEqual(item["call_sites"][0]["written_arguments"], 1)
                    self.assertEqual(item["call_sites"][0]["evidence"], "static")

    def test_call_site_marks_truncated_source_and_callee(self):
        with tempfile.TemporaryDirectory() as tmp:
            name = "x" * 220
            (Path(tmp) / "m.py").write_text("def f():\n    " + " " * 1000 + name + "()\n", encoding="utf-8")
            scan = scan_repository(tmp, limits=Limits(isolation="portable"))
            item = next(n for n in scan["nodes"] if n["id"] == "function:m.py:f")
            call = item["call_sites"][0]
            self.assertTrue(call["written_truncated"])
            self.assertTrue(call["snippet_truncated"])
            self.assertEqual(len(call["written"]), 200)

    def test_python_callee_spans_handle_unicode_and_multiline_syntax(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "m.py").write_text("def f(obj):\n    result = '🌟' and café(1)\n    return (\n        obj\n        .work\n    )(2)\n", encoding="utf-8")
            scan = scan_repository(tmp, limits=Limits(isolation="portable"))
            item = next(n for n in scan["nodes"] if n["id"] == "function:m.py:f")
            self.assertEqual(item["call_sites"][0]["written"], "café")
            self.assertIn("obj\n", item["call_sites"][1]["written"])
            self.assertIn(".work", item["call_sites"][1]["written"])
            self.assertEqual(item["call_sites"][1]["resolution"], "unresolved")

    def test_call_site_limit_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"; root.mkdir()
            (root / "m.py").write_text("def f():\n" + "    unknown()\n" * 35, encoding="utf-8")
            svc = Service(Path(tmp) / "ws", limits=Limits(isolation="portable"))
            try:
                scan = svc.repository_map(str(root)); ident = "function:m.py:f"
                item = next(n for n in scan["nodes"] if n["id"] == ident)
                self.assertEqual(len(item["call_sites"]), 30)
                self.assertTrue(item["call_sites_truncated"])
                answer = svc.repository_explain(scan["repo_id"], ident, "calls", "technical", scan["scan_id"])
                self.assertEqual(len(answer["call_sites"]), 20)
                self.assertTrue(any("truncated" in f for f in answer["facts"]))
            finally:
                svc.close()

    def test_python_module_alias_is_only_an_inferred_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "tools.py").write_text("def helper(x): return x\n", encoding="utf-8")
            (root / "main.py").write_text("import tools as t\ndef go(): return t.helper(1)\n", encoding="utf-8")
            scan = scan_repository(tmp, limits=Limits(isolation="portable"))
            item = next(n for n in scan["nodes"] if n["id"] == "function:main.py:go")
            call = item["call_sites"][0]
            self.assertEqual((call["written"], call["resolution"], call["target_id"]), ("t.helper", "inferred", "function:tools.py:helper"))
            self.assertIn("runtime dispatch", call["basis"])

    def test_imported_object_method_does_not_bind_to_unrelated_module_function(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "tool.py").write_text("def method(): pass\nobj = None\n", encoding="utf-8")
            (root / "main.py").write_text("from tool import obj\ndef go():\n    obj.method()\n", encoding="utf-8")
            scan = scan_repository(tmp, limits=Limits(isolation="portable"))
            item = next(n for n in scan["nodes"] if n["id"] == "function:main.py:go")
            self.assertEqual(item["call_sites"][0]["resolution"], "unresolved")
            self.assertFalse(any(e["kind"] == "possible call" and e["source"] == item["id"] for e in scan["edges"]))

    def test_guided_call_path_can_show_an_unresolved_written_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"; root.mkdir()
            (root / "m.py").write_text("def f(obj):\n    return obj.run(7)\n", encoding="utf-8")
            svc = Service(Path(tmp) / "ws", limits=Limits(isolation="portable"))
            try:
                scan = svc.repository_map(str(root)); ident = "function:m.py:f"
                path = svc.repository_guided_path(scan["repo_id"], ident, "calls", "explore", False, scan["scan_id"])
                self.assertEqual(path["status"], "ready")
                self.assertIn("No target connection is available", path["message"])
                self.assertEqual(path["steps"][-1]["topic"], "calls")
                self.assertEqual(path["steps"][-1]["explanation"]["call_sites"][0]["resolution"], "unresolved")
                self.assertFalse(any(step["via"] and step["via"]["evidence"] == "inferred" for step in path["steps"]))
            finally:
                svc.close()

    def test_source_reader_refuses_linked_files_and_directories(self):
        from verifier_lab.repository_map import _read_source
        import os
        with tempfile.TemporaryDirectory() as tmp:
            root, outside = Path(tmp) / "root", Path(tmp) / "outside"
            root.mkdir(); outside.mkdir()
            (outside / "secret.py").write_text("private", encoding="utf-8")
            try:
                os.symlink(outside / "secret.py", root / "linked.py")
                os.symlink(outside, root / "linked_dir", target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"symlink creation unavailable: {type(exc).__name__}")
            with self.assertRaises(OSError):
                _read_source(root.resolve(), Path("linked.py"))
            with self.assertRaises(OSError):
                _read_source(root.resolve(), Path("linked_dir") / "secret.py")

    def test_cross_file_imports_and_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pkg").mkdir()
            (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
            (root / "pkg" / "tools.py").write_text("def greet():\n    return 1\n", encoding="utf-8")
            (root / "pkg" / "main.py").write_text("from .tools import greet as hello\ndef start():\n    return hello()\n", encoding="utf-8")
            result = scan_repository(tmp, limits=Limits(isolation="portable"))
            call = next(e for e in result["edges"] if e["kind"] == "possible call")
            self.assertEqual(call["source"], "function:pkg/main.py:start")
            self.assertEqual(call["target"], "function:pkg/tools.py:greet")
            self.assertEqual((call["evidence"], call["line"]), ("inferred", 3))
            self.assertIn("hello()", call["snippet"])
            self.assertTrue(any(e["kind"] == "possible local module" and e["target"] == "file:pkg/tools.py" for e in result["edges"]))

    def test_moved_label_is_not_silently_reassigned(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"; root.mkdir()
            old = root / "old.py"; old.write_text("def hello():\n    return 3\n", encoding="utf-8")
            svc = Service(Path(tmp) / "ws", limits=Limits(isolation="portable"))
            try:
                first = svc.repository_map(str(root))
                old_id = "function:old.py:hello"
                svc.repository_label(first["repo_id"], old_id, "Friendly helper", first["scan_id"])
                old.rename(root / "new.py")
                moved = svc.repository_map(str(root))
                self.assertEqual(moved["unmatched_labels"][0]["suggested_node_id"], "function:new.py:hello")
                self.assertEqual(next(n for n in moved["nodes"] if n["id"] == "function:new.py:hello")["label"], "")
                svc.repository_rebind_label(first["repo_id"], old_id, "function:new.py:hello", moved["scan_id"])
                again = svc.repository_map(str(root))
                self.assertEqual(next(n for n in again["nodes"] if n["id"] == "function:new.py:hello")["label"], "Friendly helper")
                self.assertEqual(again["unmatched_labels"], [])
            finally:
                svc.close()

    @unittest.skipUnless(__import__("sys").platform == "win32", "Windows fail-closed check")
    def test_required_parser_does_not_downgrade(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "m.py").write_text("def f(): pass", encoding="utf-8")
            with self.assertRaises(ExtractionFailure) as caught:
                scan_repository(tmp)
            self.assertEqual(caught.exception.code, "isolation_unavailable")

    def test_repository_move_requires_explicit_reconnection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "original"; root.mkdir()
            (root / "m.py").write_text("def start(): pass\n", encoding="utf-8")
            svc = Service(Path(tmp) / "ws", limits=Limits(isolation="portable"))
            try:
                first = svc.repository_map(str(root))
                svc.repository_label(first["repo_id"], "function:m.py:start", "My entry", first["scan_id"])
                moved = Path(tmp) / "moved"; root.rename(moved)
                fresh = svc.repository_map(str(moved))
                self.assertNotEqual(first["repo_id"], fresh["repo_id"])
                self.assertEqual(next(n for n in fresh["nodes"] if n["id"] == "function:m.py:start")["label"], "")
                self.assertIn(first["repo_id"], [p["id"] for p in fresh["saved_projects"]])
                svc.repository_reconnect(fresh["location_id"], first["repo_id"], fresh["scan_id"])
                restored = svc.repository_map(str(moved))
                self.assertEqual(restored["repo_id"], first["repo_id"])
                self.assertEqual(next(n for n in restored["nodes"] if n["id"] == "function:m.py:start")["label"], "My entry")
            finally:
                svc.close()

    def test_review_marks_follow_explicit_repository_reconnection(self):
        with tempfile.TemporaryDirectory() as tmp:
            original = Path(tmp) / "before"; original.mkdir()
            (original / "m.py").write_text("def f(): pass\n", encoding="utf-8")
            svc = Service(Path(tmp) / "ws", limits=Limits(isolation="portable"))
            try:
                first = svc.repository_map(str(original))
                svc.repository_progress(first["repo_id"], "function:m.py:f", True, first["scan_id"])
                moved = Path(tmp) / "after"; original.rename(moved)
                second = svc.repository_map(str(moved))
                saved = next(p for p in second["saved_projects"] if p["id"] == first["repo_id"])
                self.assertEqual((saved["labels"], saved["progress"]), (0, 1))
                svc.repository_reconnect(second["location_id"], first["repo_id"], second["scan_id"])
                restored = svc.repository_map(str(moved))
                self.assertTrue(next(n for n in restored["nodes"] if n["id"] == "function:m.py:f")["reviewed"])
                with self.assertRaises(ServiceError):
                    svc.repository_progress(second["repo_id"], "function:m.py:f", True, second["scan_id"])
            finally:
                svc.close()

    def test_reconnection_does_not_merge_existing_labels(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = Service(Path(tmp) / "ws", limits=Limits(isolation="portable"))
            try:
                a, b = Path(tmp) / "a", Path(tmp) / "b"
                a.mkdir(); b.mkdir()
                for folder in (a,b):
                    (folder / "m.py").write_text("def f(): pass\n", encoding="utf-8")
                first, second = svc.repository_map(str(a)), svc.repository_map(str(b))
                svc.repository_label(first["repo_id"], "function:m.py:f", "First", first["scan_id"])
                svc.repository_label(second["repo_id"], "function:m.py:f", "Second", second["scan_id"])
                with self.assertRaises(ServiceError) as error:
                    svc.repository_reconnect(second["location_id"], first["repo_id"], second["scan_id"])
                self.assertEqual(error.exception.code, "label_conflict")
            finally:
                svc.close()

    def test_evidence_practice_for_call_definition_and_import(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"; root.mkdir()
            (root / "m.py").write_text("import json\ndef helper(): pass\ndef start():\n    helper()\n", encoding="utf-8")
            svc = Service(Path(tmp) / "ws", limits=Limits(isolation="portable"))
            try:
                result = svc.repository_map(str(root))
                node = "function:m.py:start"
                good = svc.repository_practice(result["repo_id"], node, "source", result["scan_id"], "call")
                bad = svc.repository_practice(result["repo_id"], node, "ran", result["scan_id"], "call")
                self.assertTrue(good["correct"])
                self.assertFalse(bad["correct"])
                self.assertEqual((good["evidence"]["path"], good["evidence"]["line"]), ("m.py", 4))
                self.assertIn("helper()", good["evidence"]["snippet"])
                self.assertTrue(svc.repository_practice(result["repo_id"], node, "source", result["scan_id"], "definition")["correct"])
                dependency = next(n for n in result["nodes"] if n["kind"] == "dependency")
                self.assertTrue(svc.repository_practice(result["repo_id"], dependency["id"], "source", result["scan_id"], "import")["correct"])
                with self.assertRaises(ServiceError):
                    svc.repository_practice(result["repo_id"], "function:m.py:helper", "source", result["scan_id"], "call")
            finally:
                svc.close()

    def test_typescript_tsx_cross_file_source_evidence(self):
        try:
            import tree_sitter, tree_sitter_typescript
        except ImportError:
            self.skipTest("optional language parsers are not installed")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "tools.ts").write_text("export function greet(name: string): string { return name; }\n", encoding="utf-8")
            (root / "view.tsx").write_text('import {greet as hello} from "./tools";\nexport function App() { return <button onClick={() => hello("x")}>Hi</button>; }\n', encoding="utf-8")
            (root / "broken.ts").write_text("export function broken( {", encoding="utf-8")
            (root / "component.jsx").write_text("export function Widget() { return <div/>; }", encoding="utf-8")
            data = scan_repository(tmp, limits=Limits(isolation="portable"))
            edge = next(e for e in data["edges"] if e["kind"] == "possible call")
            self.assertEqual((edge["source"],edge["target"],edge["evidence"]),
                             ("function:view.tsx:App", "function:tools.ts:greet", "inferred"))
            self.assertIn("hello", edge["snippet"])
            self.assertTrue(any(n["name"] == "Widget" and n["path"] == "component.jsx" for n in data["nodes"]))
            self.assertTrue(any(e["kind"] == "possible local module" and e["target"] == "file:tools.ts" for e in data["edges"]))
            self.assertTrue(any("broken.ts" in w for w in data["warnings"]))

    def test_partial_parse_keeps_only_clean_declarations(self):
        try:
            import tree_sitter, tree_sitter_typescript
        except ImportError:
            self.skipTest("optional TypeScript parser is not installed")
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "partial.ts").write_text("export function clean(){ return 1; }\nexport function broken( {\n", encoding="utf-8")
            result = scan_repository(tmp, limits=Limits(isolation="portable"))
            names = [n["name"] for n in result["nodes"] if n["kind"] == "function"]
            self.assertIn("clean", names)
            self.assertNotIn("broken", names)
            self.assertTrue(any("Partial syntax coverage" in w for w in result["warnings"]))

    def test_cpp_include_and_unique_name_are_not_runtime_observations(self):
        try:
            import tree_sitter, tree_sitter_cpp
        except ImportError:
            self.skipTest("optional C++ parser is not installed")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "helpers.hpp").write_text("inline int helper() { return 1; }\n", encoding="utf-8")
            (root / "main.cpp").write_text('#include "helpers.hpp"\nint main() { return helper(); }\n', encoding="utf-8")
            data = scan_repository(tmp, limits=Limits(isolation="portable"))
            edge = next(e for e in data["edges"] if e["kind"] == "possible call")
            self.assertEqual((edge["source"],edge["target"],edge["evidence"]),
                             ("function:main.cpp:main", "function:helpers.hpp:helper", "inferred"))
            self.assertIn("compilation and linking", edge["basis"])
            self.assertTrue(any(e["kind"] == "possible local module" and e["target"] == "file:helpers.hpp" for e in data["edges"]))
            self.assertFalse(any(e["evidence"] == "observed" for e in data["edges"]))
            svc = Service(root / "workbench", limits=Limits(isolation="portable"))
            try:
                mapped = svc.repository_map(str(root))
                include = next(n for n in mapped["nodes"] if n["kind"] == "dependency" and n["path"] == "main.cpp")
                self.assertTrue(svc.repository_practice(mapped["repo_id"], include["id"], "source", mapped["scan_id"], "import")["correct"])
            finally:
                svc.close()

    def test_cpp_overloads_do_not_become_unique_call_targets(self):
        try:
            import tree_sitter, tree_sitter_cpp
        except ImportError:
            self.skipTest("optional C++ parser is not installed")
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "main.cpp").write_text("int f(int x){return x;}\nint f(double x){return 1;}\nint g(){return f(1);}\n", encoding="utf-8")
            result = scan_repository(tmp, limits=Limits(isolation="portable"))
            self.assertFalse(any(e["kind"] == "possible call" for e in result["edges"]))

    def test_typescript_type_only_import_is_not_a_call_binding(self):
        try:
            import tree_sitter, tree_sitter_typescript
        except ImportError:
            self.skipTest("optional TypeScript parser is not installed")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "lib.ts").write_text("export function helper(){ return 1; }\n", encoding="utf-8")
            (root / "main.ts").write_text('import type { helper } from "./lib";\nexport function f(){ return helper(); }\n', encoding="utf-8")
            result = scan_repository(tmp, limits=Limits(isolation="portable"))
            self.assertFalse(any(e["kind"] == "possible call" for e in result["edges"]))
            self.assertTrue(any(e["kind"] == "possible local module" for e in result["edges"]))

    def test_javascript_cross_file_and_current_syntax(self):
        try:
            import tree_sitter, tree_sitter_typescript
        except ImportError:
            self.skipTest("optional JavaScript parser is not installed")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "tools.js").write_text("export function greet() { return 1; }\nfunction privateFn() {}\n", encoding="utf-8")
            (root / "main.js").write_text('import {greet as hi, privateFn} from "./tools.js";\nexport const start = () => hi();\n// privateFn()\n', encoding="utf-8")
            (root / "unsupported.js").write_text("const x = obj?.name;", encoding="utf-8")
            result = scan_repository(tmp, limits=Limits(isolation="portable"))
            edge = next(e for e in result["edges"] if e["kind"] == "possible call")
            self.assertEqual(edge["source"], "function:main.js:start")
            self.assertEqual(edge["target"], "function:tools.js:greet")
            self.assertEqual(edge["evidence"], "inferred")
            self.assertTrue(any(e["kind"] == "possible local module" and e["target"] == "file:tools.js" for e in result["edges"]))
            # Optional chaining used to stop the older parser; the file is now read rather than dropped.
            self.assertTrue(any(n["id"] == "file:unsupported.js" for n in result["nodes"]))
            self.assertFalse(any("unsupported.js" in w for w in result["warnings"]))
            self.assertFalse(any("privateFn()" in e.get("snippet", "") for e in result["edges"]))

    def test_explanations_use_source_shape_and_skip_nested_returns(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"; root.mkdir()
            (root / "m.py").write_text("async def combine(a, /, b, *rest, key, **kw):\n    def nested():\n        return 99\n    if a:\n        return b\n    return None\n", encoding="utf-8")
            svc = Service(Path(tmp) / "ws", limits=Limits(isolation="portable"))
            try:
                scan = svc.repository_map(str(root)); ident = "function:m.py:combine"
                node = next(n for n in scan["nodes"] if n["id"] == ident)
                self.assertEqual([p["name"] for p in node["shape"]["parameters"]], ["a", "b", "rest", "key", "kw"])
                self.assertEqual(node["shape"]["return_lines"], [5, 6])
                self.assertTrue(node["shape"]["async"])
                inputs = svc.repository_explain(scan["repo_id"], ident, "inputs", "beginner", scan["scan_id"])
                self.assertTrue(any("a, b, rest, key, kw" in fact for fact in inputs["facts"]))
                self.assertEqual(inputs["source"]["sha256"], node["sha256"])
                returns = svc.repository_explain(scan["repo_id"], ident, "returns", "technical", scan["scan_id"])
                self.assertTrue(any("5, 6" in fact for fact in returns["facts"]))
                self.assertFalse(any("line 3" in fact or "lines 3" in fact for fact in returns["facts"]))
                self.assertEqual([c["line"] for c in returns["citations"]], [5, 6])
                self.assertIn("return b", returns["citations"][0]["text"])
                self.assertIn("not purpose", returns["limitation"])
            finally:
                svc.close()

    def test_guided_path_uses_only_explicit_current_review_marks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"; root.mkdir()
            (root / "a.py").write_text("def first(): pass\n", encoding="utf-8")
            (root / "b.py").write_text("def second(): pass\n", encoding="utf-8")
            svc = Service(Path(tmp) / "ws", limits=Limits(isolation="portable"))
            try:
                scan = svc.repository_map(str(root)); sid = scan["scan_id"]
                svc.repository_progress(scan["repo_id"], "function:a.py:first", True, sid)
                plain = svc.repository_guided_path(scan["repo_id"], "repo", "understand", "beginner", False, sid)
                adapted = svc.repository_guided_path(scan["repo_id"], "repo", "understand", "beginner", True, sid)
                self.assertEqual(plain["steps"][-1]["node_id"], "function:a.py:first")
                self.assertEqual(adapted["steps"][-1]["node_id"], "function:b.py:second")
                self.assertFalse(next(n for n in scan["nodes"] if n["id"] == "function:b.py:second")["reviewed"])
                self.assertEqual(len(svc.store.q("SELECT node_id FROM repository_progress WHERE repo_id=?", (scan["repo_id"],))), 1)
                self.assertIn("not test scores", adapted["message"])
            finally:
                svc.close()

    def test_control_flow_path_cites_written_constructs_not_nested_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"; root.mkdir()
            (root / "m.py").write_text("def work(x):\n    if x:\n        for y in x:\n            pass\n    def inner():\n        while True:\n            pass\n    return x\n", encoding="utf-8")
            svc = Service(Path(tmp) / "ws", limits=Limits(isolation="portable"))
            try:
                scan = svc.repository_map(str(root)); ident = "function:m.py:work"
                answer = svc.repository_explain(scan["repo_id"], ident, "structure", "beginner", scan["scan_id"])
                self.assertTrue(any("conditional on line 2" in f and "loop on line 3" in f for f in answer["facts"]))
                self.assertFalse(any("line 6" in f for f in answer["facts"]))
                self.assertEqual([c["line"] for c in answer["citations"]], [2, 3])
                self.assertTrue(any("does not prove it ran" in f for f in answer["facts"]))
                path = svc.repository_guided_path(scan["repo_id"], ident, "understand", "explore", False, scan["scan_id"])
                self.assertIn("structure", [step["topic"] for step in path["steps"]])
            finally:
                svc.close()

    def test_javascript_and_cpp_structure_projections(self):
        try:
            import esprima, tree_sitter, tree_sitter_cpp
        except ImportError:
            self.skipTest("optional parsers are not installed")
        from verifier_lab.repository_javascript import parse_javascript
        from verifier_lab.repository_trees import parse_source
        js = parse_javascript(b"function f(x) { if(x) { return x; } }", "m.js")
        cpp = parse_source(b"int f(int x) { while(x) { --x; } return x; }", "m.cpp", "cpp")
        self.assertEqual(js["nodes"][0]["shape"]["features"][0]["kind"], "conditional")
        self.assertEqual(cpp["nodes"][0]["shape"]["features"][0]["kind"], "loop")

    def test_explanation_cites_return_beyond_preview(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"; root.mkdir()
            (root / "long.py").write_text("def f(x):\n" + "    pass\n" * 20 + "    return x\n", encoding="utf-8")
            svc = Service(Path(tmp) / "ws", limits=Limits(isolation="portable"))
            try:
                scan = svc.repository_map(str(root)); ident = "function:long.py:f"
                item = next(n for n in scan["nodes"] if n["id"] == ident)
                self.assertNotIn("return x", item["snippet"])
                answer = svc.repository_explain(scan["repo_id"], ident, "returns", "beginner", scan["scan_id"])
                self.assertEqual(answer["citations"][0]["line"], 22)
                self.assertIn("return x", answer["citations"][0]["text"])
                self.assertEqual(answer["source"]["sha256"], item["sha256"])
            finally:
                svc.close()

    def test_review_marks_are_opt_in_stale_and_clearable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, workspace = Path(tmp) / "repo", Path(tmp) / "ws"
            root.mkdir(); path = root / "m.py"; path.write_text("def f(x):\n    return x\n", encoding="utf-8")
            svc = Service(workspace, limits=Limits(isolation="portable"))
            try:
                first = svc.repository_map(str(root)); ident = "function:m.py:f"
                self.assertFalse(next(n for n in first["nodes"] if n["id"] == ident)["reviewed"])
                svc.repository_progress(first["repo_id"], ident, True, first["scan_id"])
                svc.repository_label(first["repo_id"], ident, "My function", first["scan_id"])
            finally:
                svc.close()
            svc = Service(workspace, limits=Limits(isolation="portable"))
            try:
                second = svc.repository_map(str(root))
                self.assertTrue(next(n for n in second["nodes"] if n["id"] == ident)["reviewed"])
                path.write_text("def f(x):\n    return x + 1\n", encoding="utf-8")
                changed = svc.repository_map(str(root))
                self.assertTrue(next(n for n in changed["nodes"] if n["id"] == ident)["review_needs_refresh"])
                with self.assertRaises(ServiceError) as old:
                    svc.repository_explain(second["repo_id"], ident, "inputs", "beginner", second["scan_id"])
                self.assertEqual(old.exception.code, "stale_repository")
                with self.assertRaises(ServiceError):
                    svc.repository_progress(second["repo_id"], ident, True, second["scan_id"])
                with self.assertRaises(ServiceError):
                    svc.repository_label(second["repo_id"], ident, "Wrong snapshot", second["scan_id"])
                svc.repository_progress_clear(changed["repo_id"], changed["scan_id"])
                cleared = svc.repository_map(str(root))
                item = next(n for n in cleared["nodes"] if n["id"] == ident)
                self.assertFalse(item["reviewed"])
                self.assertEqual(item["label"], "My function")
            finally:
                svc.close()

    def test_cpp_explanation_preserves_parameter_and_return_evidence(self):
        try:
            import tree_sitter, tree_sitter_cpp
        except ImportError:
            self.skipTest("optional C++ parser is not installed")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"; root.mkdir()
            (root / "m.cpp").write_text("int sum(int left, int right) {\n    return left + right;\n}\n", encoding="utf-8")
            svc = Service(Path(tmp) / "ws", limits=Limits(isolation="portable"))
            try:
                scan = svc.repository_map(str(root)); ident = "function:m.cpp:sum"
                answer = svc.repository_explain(scan["repo_id"], ident, "inputs", "technical", scan["scan_id"])
                self.assertTrue(any("left" in f and "right" in f for f in answer["facts"]))
                ret = svc.repository_explain(scan["repo_id"], ident, "returns", "technical", scan["scan_id"])
                self.assertTrue(any("line 2" in f for f in ret["facts"]))
                self.assertEqual(ret["source"]["path"], "m.cpp")
            finally:
                svc.close()

    def test_typescript_arrow_explanation_does_not_deny_implicit_returns(self):
        try:
            import tree_sitter, tree_sitter_typescript
        except ImportError:
            self.skipTest("optional TypeScript parser is not installed")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"; root.mkdir()
            (root / "m.ts").write_text("export const twice = (x: number) => x * 2;\n", encoding="utf-8")
            svc = Service(Path(tmp) / "ws", limits=Limits(isolation="portable"))
            try:
                scan = svc.repository_map(str(root)); ident = "function:m.ts:twice"
                node = next(n for n in scan["nodes"] if n["id"] == ident)
                self.assertEqual(node["shape"]["parameters"][0]["name"], "x")
                answer = svc.repository_explain(scan["repo_id"], ident, "returns", "beginner", scan["scan_id"])
                self.assertTrue(any("arrow function may return" in f for f in answer["facts"]))
            finally:
                svc.close()

    def test_network_paths_rejected_before_access(self):
        with self.assertRaisesRegex(ValueError, "Network paths"):
            scan_repository("//server/share/project")

    def test_ambiguous_definition_does_not_resolve(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "m.py").write_text("def f(): pass\ndef f(): pass\ndef g(): f()\n", encoding="utf-8")
            result = scan_repository(tmp, limits=Limits(isolation="portable"))
            self.assertFalse(any(e["kind"] == "possible call" for e in result["edges"]))


class RepositoryApiTests(ServerCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.svc = Service(Path(cls.tmp.name), limits=Limits(isolation="portable"))
        cls.wb = Workbench(cls.svc, "127.0.0.1", 0)
        cls.port = cls.wb.server_port
        cls.thread = threading.Thread(target=cls.wb.serve_forever, daemon=True)
        cls.thread.start()

    def test_explain_and_progress_require_current_snapshot_and_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root / "m.py").write_text("def f(value):\n    return value\n", encoding="utf-8")
            status, scan, _ = self.call("POST", "/api/v1/repository/scan", {"path": str(root), "content_access": "adult"})
            self.assertEqual(status, 200)
            body = {"repo_id": scan["repo_id"], "node_id": "function:m.py:f", "topic": "inputs", "level": "beginner", "scan_id": scan["scan_id"]}
            self.assertEqual(self.call("POST", "/api/v1/repository/explain", body, token=False)[0], 401)
            status, explanation, _ = self.call("POST", "/api/v1/repository/explain", body)
            self.assertEqual(status, 200)
            self.assertTrue(any("value" in fact for fact in explanation["facts"]))
            self.assertEqual(explanation["source"]["path"], "m.py")
            self.assertEqual(explanation["provider"]["mode"], "local")
            citation = explanation["evidence_cards"][0]["citation"]
            self.assertEqual((citation["path"], citation["line"], citation["text"]), ("m.py", 1, "def f(value):"))
            self.assertEqual(citation["sha256"], explanation["source"]["sha256"])
            mark = {"repo_id": scan["repo_id"], "node_id": "function:m.py:f", "reviewed": True, "scan_id": scan["scan_id"]}
            self.assertEqual(self.call("POST", "/api/v1/repository/progress", mark)[0], 200)
            _, current, _ = self.call("POST", "/api/v1/repository/scan", {"path": str(root), "content_access": "adult"})
            self.assertTrue(next(n for n in current["nodes"] if n["id"] == "function:m.py:f")["reviewed"])
            self.assertEqual(self.call("POST", "/api/v1/repository/explain", body)[0], 409)
            self.assertEqual(self.call("POST", "/api/v1/repository/progress", mark)[0], 409)
            path_body = {"repo_id": current["repo_id"], "node_id": "repo", "goal": "understand", "level": "beginner", "prefer_unreviewed": True, "scan_id": current["scan_id"]}
            self.assertEqual(self.call("POST", "/api/v1/repository/guided-path", path_body, token=False)[0], 401)
            status, path, _ = self.call("POST", "/api/v1/repository/guided-path", path_body)
            self.assertEqual(status, 200)
            self.assertEqual(path["status"], "ready")
            self.assertEqual(path["steps"][-1]["topic"], "returns")
            self.assertEqual(path["steps"][-1]["explanation"]["provider"]["mode"], "local")
            self.assertEqual(path["steps"][-1]["explanation"]["evidence_cards"][0]["citation"]["line"], 2)
            path_body["scan_id"] = scan["scan_id"]
            self.assertEqual(self.call("POST", "/api/v1/repository/guided-path", path_body)[0], 409)

    def test_model_opt_in_through_api_respects_adult_session(self):
        class FakeLocalAssistant:
            calls = 0
            def available(self): return True
            def safe_for(self, path): return True
            def generate(self, cards, topic, level):
                self.calls += 1
                return {"text": "A return is written [1].", "citations": [{"id": 1, **cards[0]["citation"]}]}
        fake, previous = FakeLocalAssistant(), self.svc.local_assistant
        self.svc.local_assistant = fake
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); (root / "m.py").write_text("def f():\n    return 1\n", encoding="utf-8")
                _, scan, _ = self.call("POST", "/api/v1/repository/scan", {"path": str(root), "content_access": "adult"})
                body = {"repo_id": scan["repo_id"], "node_id": "function:m.py:f", "topic": "returns", "level": "beginner", "scan_id": scan["scan_id"], "use_local_assistant": True}
                self.assertEqual(self.call("POST", "/api/v1/repository/explain", body, token=False)[0], 401)
                status, answer, _ = self.call("POST", "/api/v1/repository/explain", body)
                self.assertEqual((status, answer["assistant"]["status"], fake.calls), (200, "ready", 1))
                self.assertEqual(answer["assistant"]["citations"][0]["line"], 2)
                _, child, _ = self.call("POST", "/api/v1/repository/scan", {"path": str(root), "content_access": "adult_reviewed"})
                body.update(repo_id=child["repo_id"], scan_id=child["scan_id"])
                self.assertEqual(self.call("POST", "/api/v1/repository/explain", body)[0], 403)
                self.assertEqual(fake.calls, 1)
        finally:
            self.svc.local_assistant = previous

    def test_label_path_http_requires_session_label_and_current_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root / "m.py").write_text("def helper(): return 1\ndef start(): return helper()\n", encoding="utf-8")
            _, scan, _ = self.call("POST", "/api/v1/repository/scan", {"path": str(root), "content_access": "adult"})
            body = {"repo_id": scan["repo_id"], "node_id": "function:m.py:start", "target_id": "function:m.py:helper",
                    "level": "beginner", "include_inferred": True, "include_organization": False, "scan_id": scan["scan_id"]}
            self.assertEqual(self.call("POST", "/api/v1/repository/label-path", body, token=False)[0], 401)
            self.assertEqual(self.call("POST", "/api/v1/repository/label-path", body)[0], 409)
            self.assertEqual(self.call("POST", "/api/v1/repository/label", {"repo_id": scan["repo_id"], "node_id": body["target_id"], "label": "Helper", "scan_id": scan["scan_id"]})[0], 200)
            status, route, _ = self.call("POST", "/api/v1/repository/label-path", body)
            self.assertEqual((status, route["status"], route["steps"][-1]["via"]["evidence"]), (200, "ready", "inferred"))
            body["include_inferred"] = False
            self.assertEqual(self.call("POST", "/api/v1/repository/label-path", body)[1]["status"], "unavailable")
            self.call("POST", "/api/v1/repository/scan", {"path": str(root), "content_access": "adult"})
            self.assertEqual(self.call("POST", "/api/v1/repository/label-path", body)[0], 409)

    def test_shelf_requires_adult_choice_and_remembered_current_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root / "m.py").write_text("def f(): pass\n", encoding="utf-8")
            self.assertEqual(self.call("POST", "/api/v1/repository/shelf", {"content_access": "adult"}, token=False)[0], 401)
            self.assertEqual(self.call("POST", "/api/v1/repository/shelf", {"content_access": "adult_reviewed"})[0], 403)
            _, child, _ = self.call("POST", "/api/v1/repository/scan", {"path": str(root), "content_access": "adult_reviewed"})
            req = {"repo_id": child["repo_id"], "scan_id": child["scan_id"]}
            self.assertEqual(self.call("POST", "/api/v1/repository/remember", req)[0], 403)
            _, adult, _ = self.call("POST", "/api/v1/repository/scan", {"path": str(root), "content_access": "adult"})
            self.assertEqual(self.call("POST", "/api/v1/repository/remember", req)[0], 409)
            req["scan_id"] = adult["scan_id"]
            self.assertEqual(self.call("POST", "/api/v1/repository/remember", req)[0], 200)
            status, shelf, _ = self.call("POST", "/api/v1/repository/shelf", {"content_access": "adult"})
            self.assertEqual(status, 200)
            self.assertTrue(any(item["location_id"] == adult["location_id"] for item in shelf["items"]))
            self.assertEqual(self.call("POST", "/api/v1/repository/forget", {"content_access": "adult_reviewed", "location_id": adult["location_id"]})[0], 403)
            self.assertEqual(self.call("POST", "/api/v1/repository/forget", {"content_access": "adult", "location_id": adult["location_id"]})[0], 200)

    def test_compare_call_requires_session_and_current_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root / "m.py").write_text("def f(x): return x\ndef g(): return f(1)\n", encoding="utf-8")
            _, scan, _ = self.call("POST", "/api/v1/repository/scan", {"path": str(root), "content_access": "adult"})
            body = {"repo_id": scan["repo_id"], "node_id": "function:m.py:g", "call_index": 0,
                    "level": "explore", "scan_id": scan["scan_id"]}
            self.assertEqual(self.call("POST", "/api/v1/repository/compare-call", body, token=False)[0], 401)
            status, result, _ = self.call("POST", "/api/v1/repository/compare-call", body)
            self.assertEqual((status, result["relationship"]["evidence"], result["target"]["node_id"]), (200, "inferred", "function:m.py:f"))
            self.call("POST", "/api/v1/repository/scan", {"path": str(root), "content_access": "adult"})
            self.assertEqual(self.call("POST", "/api/v1/repository/compare-call", body)[0], 409)

    def test_connected_repository_learning_workflow(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"; root.mkdir()
            marker = Path(tmp) / "executed"
            (root / "lib.py").write_text("def helper(name):\n    return name\n", encoding="utf-8")
            (root / "main.py").write_text(f"from pathlib import Path\nPath({str(marker)!r}).touch()\nfrom lib import helper\ndef start(value):\n    return helper(value)\n", encoding="utf-8")
            status, scan, _ = self.call("POST", "/api/v1/repository/scan", {"path": str(root), "content_access": "adult"})
            self.assertEqual(status, 200)
            self.assertFalse(marker.exists())
            repo, sid = scan["repo_id"], scan["scan_id"]
            file = next(n for n in scan["nodes"] if n["id"] == "file:main.py")
            self.assertEqual(file["source_preview"][3]["text"], "def start(value):")
            target, origin = "function:lib.py:helper", "function:main.py:start"
            self.assertEqual(self.call("POST", "/api/v1/repository/label", {"repo_id": repo, "node_id": target, "label": "My helper", "scan_id": sid})[0], 200)
            self.assertEqual(self.call("POST", "/api/v1/repository/remember", {"repo_id": repo, "scan_id": sid})[0], 200)
            body = {"repo_id": repo, "node_id": origin, "call_index": 0, "level": "beginner", "scan_id": sid}
            status, comparison, _ = self.call("POST", "/api/v1/repository/compare-call", body)
            self.assertEqual((status, comparison["target"]["node_id"], comparison["relationship"]["evidence"]), (200, target, "inferred"))
            route_body = {"repo_id": repo, "node_id": origin, "target_id": target, "level": "beginner",
                          "include_inferred": True, "include_organization": False, "scan_id": sid}
            status, route, _ = self.call("POST", "/api/v1/repository/label-path", route_body)
            self.assertEqual((status, route["steps"][-1]["node_id"], route["steps"][-1]["via"]["evidence"]), (200, target, "inferred"))
            _, reopened, _ = self.call("POST", "/api/v1/repository/scan", {"path": str(root), "content_access": "adult"})
            self.assertEqual(next(n for n in reopened["nodes"] if n["id"] == target)["label"], "My helper")
            self.assertEqual(self.call("POST", "/api/v1/repository/compare-call", body)[0], 409)
            self.assertFalse(marker.exists())
            self.assertEqual(self.call("POST", "/api/v1/repository/forget", {"content_access": "adult", "location_id": scan["location_id"]})[0], 200)

    def test_adult_reviewed_view_hides_other_repository_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            first, second = Path(tmp) / "first", Path(tmp) / "second"
            first.mkdir(); second.mkdir()
            for folder in (first, second):
                (folder / "m.py").write_text("def f(): pass\n", encoding="utf-8")
            _, data, _ = self.call("POST", "/api/v1/repository/scan", {"path": str(first), "content_access": "adult"})
            self.call("POST", "/api/v1/repository/label", {"repo_id": data["repo_id"], "node_id": "function:m.py:f", "label": "Saved", "scan_id": data["scan_id"]})
            _, adult, _ = self.call("POST", "/api/v1/repository/scan", {"path": str(second), "content_access": "adult"})
            self.assertTrue(adult["saved_projects"])
            _, reviewed, _ = self.call("POST", "/api/v1/repository/scan", {"path": str(second), "content_access": "adult_reviewed"})
            self.assertEqual(reviewed["saved_projects"], [])

    def test_scan_and_label_use_existing_security_guard(self):
        root = Path(__file__).resolve().parents[2] / "examples"
        self.assertEqual(self.call("POST", "/api/v1/repository/scan", {"path": str(root), "content_access": "child"})[0], 403)
        self.assertEqual(self.call("POST", "/api/v1/repository/scan", {"path": str(root)})[0], 422)
        self.assertEqual(self.call("POST", "/api/v1/repository/scan", {"path": str(root), "content_access": "adult"}, token=False)[0], 401)
        self.assertEqual(self.call("POST", "/api/v1/repository/scan", {"path": str(root), "content_access": "adult"}, headers={"Origin": "http://evil.example"})[0], 403)
        status, result, _ = self.call("POST", "/api/v1/repository/scan", {"path": str(root), "content_access": "adult"})
        self.assertEqual(status, 200)
        node = next(n for n in result["nodes"] if n["kind"] == "function")
        payload = {"repo_id": result["repo_id"], "node_id": node["id"], "label": "Start here", "scan_id": result["scan_id"]}
        self.assertEqual(self.call("POST", "/api/v1/repository/label", payload)[0], 200)
        _, rescanned, _ = self.call("POST", "/api/v1/repository/scan", {"path": str(root), "content_access": "adult"})
        self.assertEqual(next(n for n in rescanned["nodes"] if n["id"] == node["id"])["label"], "Start here")

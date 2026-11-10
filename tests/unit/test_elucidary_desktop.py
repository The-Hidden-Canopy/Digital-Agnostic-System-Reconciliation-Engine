import socket
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from verifier_lab.elucidary_desktop import DesktopApp, DesktopController, describe_connection, format_explanation, format_repository_overview, graph_constellations, graph_labels, graph_view
from verifier_lab.ingestion.common import Limits
from verifier_lab.service import Service, ServiceError


class DesktopControllerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root_path = Path(self.tmp.name) / "repo"; self.root_path.mkdir()
        self.marker = Path(self.tmp.name) / "executed"
        (self.root_path / "lib.py").write_text("def helper(value):\n    return value\n", encoding="utf-8")
        (self.root_path / "main.py").write_text(f"from pathlib import Path\nPath({str(self.marker)!r}).touch()\nfrom lib import helper\ndef start(value):\n    return helper(value)\n", encoding="utf-8")
        self.service = Service(Path(self.tmp.name) / "workspace", limits=Limits(isolation="portable"))
        self.addCleanup(self.service.close)
        self.ctrl = DesktopController(self.service)

    def test_desktop_controller_has_a_connected_local_workflow_without_sockets(self):
        with patch.object(socket, "socket", side_effect=AssertionError("no server should start")):
            scan = self.ctrl.open(str(self.root_path), "adult", "portable")
            self.assertFalse(self.marker.exists())
            self.assertTrue(next(n for n in scan["nodes"] if n["id"] == "file:main.py")["source_preview"])
            self.ctrl.select("function:lib.py:helper"); self.ctrl.label("My helper"); self.ctrl.remember()
            self.ctrl.select("function:main.py:start")
            comparison = self.ctrl.compare(0, "beginner")
            self.assertEqual((comparison["target"]["node_id"], comparison["relationship"]["evidence"]), ("function:lib.py:helper", "inferred"))
            route = self.ctrl.path_to_label("function:lib.py:helper", "beginner", True)
            self.assertEqual(route["status"], "ready")
            self.assertEqual(route["steps"][-1]["via"]["evidence"], "inferred")
            self.assertTrue(self.ctrl.shelf("adult"))
            reopened = self.ctrl.open(str(self.root_path), "adult", "portable")
            self.assertEqual(next(n for n in reopened["nodes"] if n["id"] == "function:lib.py:helper")["label"], "My helper")
            self.assertFalse(self.marker.exists())

    def test_child_boundary_and_required_isolation_are_not_silently_relaxed(self):
        with self.assertRaises(ServiceError): self.ctrl.open(str(self.root_path), "child", "portable")
        with self.assertRaises(ServiceError): self.ctrl.shelf("adult_reviewed")
        with patch("verifier_lab.repository_map.scan_repository", side_effect=AssertionError("unexpected"), create=True):
            with self.assertRaises(ServiceError): self.ctrl.open(str(self.root_path), "adult", "unknown")

    def test_graph_selection_is_bounded_and_retains_path(self):
        nodes = [{"id": str(i), "kind": "function", "name": str(i), "path": f"{i}.py"} for i in range(120)]
        edges = [{"source": "0", "target": str(i), "evidence": "static"} for i in range(1, 120)]
        chosen, positions = graph_view(nodes, edges, "0", ["119"], limit=20)
        self.assertEqual(len(chosen), 20); self.assertIn("119", chosen)
        self.assertEqual(set(chosen), set(positions))

    def test_graph_labels_prioritize_selection_and_avoid_overlap(self):
        nodes = [{"id": "a", "kind": "function", "name": "Selected"}, {"id": "b", "kind": "function", "label": "Other"}]
        labels = graph_labels(nodes, {"a": (100, 100), "b": (100, 100)}, "a", ["b"], 500, 300)
        self.assertEqual([item[0] for item in labels], ["a"])

    def test_file_and_label_scopes_retain_routes_without_inventing_nodes(self):
        nodes = [{"id": "a", "kind": "function", "name": "a", "path": "one.py"},
                 {"id": "b", "kind": "function", "name": "b", "path": "one.py", "label": "Keep"},
                 {"id": "c", "kind": "function", "name": "c", "path": "two.py"},
                 {"id": "d", "kind": "function", "name": "d", "path": "three.py"}]
        edges = [{"source": "b", "target": "c", "evidence": "inferred"}]
        chosen, positions = graph_view(nodes, edges, "a", ["d"], limit=10, focus_path="one.py")
        self.assertEqual(set(chosen), {"a", "b", "d"})
        groups = graph_constellations(nodes, chosen, positions, "a")
        self.assertEqual(groups[0][0], "one.py"); self.assertEqual(groups[0][2], 2)
        chosen, _ = graph_view(nodes, edges, "a", limit=10, labeled=True)
        self.assertEqual(set(chosen), {"a", "b", "c"})
        chosen, _ = graph_view(nodes, [], "a", limit=10, labeled=True)
        self.assertEqual(set(chosen), {"a", "b"})

    def test_connection_explanations_keep_direction_and_uncertainty_by_level(self):
        edge = {"kind": "possible call", "evidence": "inferred"}
        beginner = describe_connection(edge, False, "beginner")
        self.assertIn("may refer", beginner); self.assertIn("points to the selected item", beginner)
        self.assertIn("not proof", beginner)
        self.assertIn("rebinding", describe_connection(edge, True, "technical"))
        self.assertIn("does not prove the code ran", describe_connection({"kind": "defines", "evidence": "static"}, True, "beginner"))

    def test_guided_call_explanation_includes_written_source_and_uncertainty(self):
        self.ctrl.open(str(self.root_path), "adult", "portable")
        self.ctrl.select("function:main.py:start")
        result = self.ctrl.explain("calls", "beginner")
        text = format_explanation(result)
        self.assertIn("Written call: helper", text)
        self.assertIn("return helper(value)", text)
        self.assertIn("possible target (inferred)", text)
        self.assertIn("Source SHA-256", text)
        self.assertIn("no runtime calls were observed", format_repository_overview(self.ctrl.data))

    def test_source_context_captures_late_items_at_scan_time(self):
        source = self.root_path / "long.py"
        source.write_text("# padding\n" * 150 + "def late(value):\n    return value\n", encoding="utf-8")
        scan = self.ctrl.open(str(self.root_path), "adult", "portable")
        file = next(n for n in scan["nodes"] if n["id"] == "file:long.py")
        self.assertFalse(any(line["line"] == 151 for line in file["source_preview"]))
        self.assertEqual(next(line["text"] for line in file["source_context"] if line["line"] == 151), "def late(value):")
        source.write_text("changed after scan\n", encoding="utf-8")
        self.assertEqual(next(line["text"] for line in file["source_context"] if line["line"] == 151), "def late(value):")

    def test_source_context_obeys_shared_scan_and_per_file_limits(self):
        source = self.root_path / "long.py"
        source.write_text("# padding\n" * 150 + "def late(value):\n    return value\n", encoding="utf-8")
        from dataclasses import replace
        from verifier_lab.repository_map import ScanLimits
        self.ctrl.scan_limits = replace(ScanLimits.desktop(), max_context_chars=20, max_context_lines_per_file=3)
        scan = self.ctrl.open(str(self.root_path), "adult", "portable")
        files = [node for node in scan["nodes"] if node["kind"] == "file"]
        self.assertLessEqual(sum(len(item["text"]) + 1 for node in files for item in node["source_context"]), 20)
        long_file = next(node for node in files if node["path"] == "long.py")
        self.assertLessEqual(len(long_file["source_context"]), 3)
        self.assertTrue(long_file["source_context_truncated"])

    def test_late_return_evidence_has_snapshot_context(self):
        source = self.root_path / "long.py"
        source.write_text("# padding\n" * 150 + "def late(value):\n" + "    # body\n" * 40 + "    return value\n", encoding="utf-8")
        scan = self.ctrl.open(str(self.root_path), "adult", "portable")
        file = next(n for n in scan["nodes"] if n["id"] == "file:long.py")
        self.assertEqual(next(line["text"] for line in file["source_context"] if line["line"] == 192), "    return value")

    def test_path_between_unlabeled_items_respects_evidence_and_current_scan(self):
        scan = self.ctrl.open(str(self.root_path), "adult", "portable")
        start, target = "function:main.py:start", "function:lib.py:helper"
        self.ctrl.select(target)
        absent = self.ctrl.path_between(start, target, "beginner", False, False)
        self.assertEqual(absent["status"], "unavailable")
        route = self.ctrl.path_between(start, target, "explore", True, False)
        self.assertEqual([step["node_id"] for step in route["steps"]], [start, target])
        self.assertEqual(route["steps"][1]["via"]["evidence"], "inferred")
        with self.assertRaises(ServiceError) as error:
            self.ctrl.path_to_label(target, "beginner", True)
        self.assertEqual(error.exception.code, "label_unavailable")
        self.ctrl.open(str(self.root_path), "adult", "portable")
        with self.assertRaises(ServiceError) as stale:
            self.service.repository_item_path(scan["repo_id"], start, target, "beginner", True, False, scan["scan_id"])
        self.assertEqual(stale.exception.code, "stale_repository")

    def test_possible_callers_use_current_scan_and_recorded_source(self):
        scan = self.ctrl.open(str(self.root_path), "adult", "portable")
        repo, sid, target = scan["repo_id"], scan["scan_id"], "function:lib.py:helper"
        with patch.object(socket, "socket", side_effect=AssertionError("unexpected socket")):
            result = self.service.repository_possible_callers(repo, target, "beginner", 1, sid)
        row = next(r for r in result["rows"] if r["caller_id"] == "function:main.py:start")
        self.assertEqual((row["path"][0]["evidence"], row["path"][0]["call_evidence"]), ("inferred", "static"))
        self.assertEqual(row["path"][0]["path"], "main.py")
        self.assertIn("helper", row["path"][0]["snippet"])
        with self.assertRaises(ServiceError): self.service.repository_possible_callers(repo, target, "beginner", True, sid)
        with self.assertRaises(ServiceError): self.service.repository_possible_callers(repo, "file:main.py", "beginner", 1, sid)
        self.ctrl.open(str(self.root_path), "adult", "portable")
        with self.assertRaises(ServiceError) as stale:
            self.service.repository_possible_callers(repo, target, "beginner", 1, sid)
        self.assertEqual(stale.exception.code, "stale_repository")

    def test_label_review_and_removal_survive_rescans(self):
        self.ctrl.open(str(self.root_path), "adult", "portable")
        ident = "function:lib.py:helper"; self.ctrl.select(ident); self.ctrl.label("My helper")
        (self.root_path / "lib.py").write_text("def helper(value):\n    return value + 1\n", encoding="utf-8")
        self.ctrl.open(str(self.root_path), "adult", "portable")
        item = self.ctrl.select(ident)
        self.assertEqual(item["label"], "My helper"); self.assertTrue(item["label_needs_review"])
        self.assertTrue(graph_labels([item], {ident: (50, 50)}, ident, [], 400, 300)[0][1].startswith("! "))
        self.ctrl.label("My helper")
        self.assertFalse(self.ctrl.nodes[ident]["label_needs_review"])
        self.ctrl.label("")
        reopened = self.ctrl.open(str(self.root_path), "adult", "portable")
        self.assertFalse(next(n for n in reopened["nodes"] if n["id"] == ident)["label"])

    def test_comparison_is_session_only_and_never_crosses_review_choice(self):
        first = self.ctrl.open(str(self.root_path), "adult", "portable")
        self.ctrl.select("function:lib.py:helper"); self.ctrl.label("My helper")
        (self.root_path / "lib.py").write_text("def helper(value):\n    return value + 1\n", encoding="utf-8")
        second = self.ctrl.open(str(self.root_path), "adult", "portable", (first, "adult"))
        changed = next(item for item in second["comparison"]["changes"] if item["node_id"] == "function:lib.py:helper")
        self.assertEqual(changed["status"], "changed"); self.assertEqual(changed["label"], "My helper")
        self.assertIn("return value", changed["before"]["excerpt"][1]["text"])
        self.assertIn("return value + 1", changed["after"]["excerpt"][1]["text"])
        self.ctrl.clear()
        self.assertNotIn("comparison", self.ctrl.open(str(self.root_path), "adult", "portable"))
        self.assertNotIn("comparison", self.ctrl.open(str(self.root_path), "adult_reviewed", "portable", (first, "adult")))

    def test_reading_practice_is_source_grounded_local_and_checks_scan(self):
        scan = self.ctrl.open(str(self.root_path), "adult", "portable")
        repo, ident, sid = scan["repo_id"], "function:lib.py:helper", scan["scan_id"]
        questions = self.service.repository_reading_questions(repo, ident, "beginner", sid)
        self.assertEqual([q["kind"] for q in questions], ["parameter", "return"])
        self.assertNotIn("correct_index", questions[0])
        answer = questions[0]["choices"].index("value")
        self.assertFalse(self.service.repository_check_reading(repo, ident, "beginner", sid, "parameter", 1 - answer)["correct"])
        checked = self.service.repository_check_reading(repo, ident, "beginner", sid, "parameter", answer)
        self.assertTrue(checked["correct"]); self.assertEqual(checked["evidence"]["path"], "lib.py")
        self.assertEqual(self.service.store.q1("SELECT COUNT(*) AS n FROM repository_progress")["n"], 0)
        self.ctrl.open(str(self.root_path), "adult", "portable")
        with self.assertRaises(ServiceError) as stale:
            self.service.repository_check_reading(repo, ident, "beginner", sid, "parameter", answer)
        self.assertEqual(stale.exception.code, "stale_repository")

    def test_reading_practice_explains_recorded_choices_without_runtime_claims(self):
        (self.root_path / "branch.py").write_text("def choose(x):\n    if x:\n        return x\n    return 0\n", encoding="utf-8")
        scan = self.ctrl.open(str(self.root_path), "adult", "portable")
        repo, ident, sid = scan["repo_id"], "function:branch.py:choose", scan["scan_id"]
        question = next(q for q in self.service.repository_reading_questions(repo, ident, "explore", sid) if q["kind"] == "structure")
        answer = next(i for i, text in enumerate(question["choices"]) if "Line 2:" in text)
        result = self.service.repository_check_reading(repo, ident, "explore", sid, "structure", answer)
        self.assertTrue(result["correct"]); self.assertEqual(result["evidence"]["text"], "    if x:")
        self.assertIn("does not show", result["explanation"])
        with self.assertRaises(ServiceError): self.service.repository_check_reading(repo, ident, "explore", sid, "structure", True)


class NativeWidgetSmokeTests(unittest.TestCase):
    def test_caller_map_discloses_when_route_uses_a_different_call(self):
        import tkinter as tk
        try:
            window = tk.Tk(); window.withdraw()
        except tk.TclError as exc:
            self.skipTest(f"Tk display unavailable: {exc}")
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"; repo.mkdir()
            (repo / "m.py").write_text("def helper(): return start()\ndef start(): return helper()\n", encoding="utf-8")
            service = Service(Path(tmp) / "ws", limits=Limits(isolation="portable"))
            try:
                controller = DesktopController(service)
                app = DesktopApp(window, controller)
                app.audience.set("Adult"); app.path.set(str(repo))
                app.loaded(controller.open(str(repo), "adult", "portable"), str(repo))
                app.select("function:m.py:helper")
                item = next(i for i, row in app.caller_ids.items() if row["caller_id"] == "function:m.py:start")
                app.caller_list.selection_set(item)
                self.assertEqual(app.selected_caller()["path"][0]["line"], 2)
                app.route_from_caller()
                via = controller.route["steps"][1]["via"]
                self.assertEqual((via["direction"], via["line"]), ("reverse", 1))
                self.assertIn("different path", controller.route["message"])
                self.assertIn("different path", app.step_text.get("1.0", "end"))
            finally:
                for callback in window.tk.splitlist(window.tk.call("after", "info")):
                    window.after_cancel(callback)
                service.close(); window.destroy()

    def test_windows_requires_explicit_trusted_repository_choice(self):
        import tkinter as tk
        try:
            window = tk.Tk(); window.withdraw()
        except tk.TclError as exc:
            self.skipTest(f"Tk display unavailable: {exc}")
        with tempfile.TemporaryDirectory() as tmp:
            service = Service(Path(tmp) / "ws", limits=Limits(isolation="portable"))
            try:
                with patch("verifier_lab.elucidary_desktop.sys.platform", "win32"):
                    app = DesktopApp(window, DesktopController(service))
                app.audience.set("Adult"); app.path.set(str(Path(tmp)))
                self.assertIsNone(app.isolation_key())
                app.open_repository()
                self.assertFalse(app.busy)
                self.assertIn("trusted repositories only", app.status.get())
                app.isolation.set("Trusted repositories only: no parser sandbox")
                self.assertEqual(app.isolation_key(), "portable")
            finally:
                service.close(); window.destroy()

    def test_native_widgets_scan_and_navigate_without_http(self):
        import tkinter as tk
        try:
            window = tk.Tk(); window.withdraw()
        except tk.TclError as exc:
            self.skipTest(f"Tk display unavailable: {exc}")
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"; repo.mkdir()
            (repo / "m.py").write_text("def helper(x): return x\ndef start(): return helper(1)\n", encoding="utf-8")
            long_source = repo / "long.py"
            long_source.write_text("# padding\n" * 150 + "def late(value):\n    return helper(value)\n", encoding="utf-8")
            service = Service(Path(tmp) / "ws", limits=Limits(isolation="portable"))
            app = DesktopApp(window, DesktopController(service))
            errors = []
            app.notify_error = errors.append
            try:
                app.audience.set("Adult"); app.audience_changed(); app.path.set(str(repo))
                app.isolation.set("Trusted development: no Linux sandbox")
                with patch.object(socket, "socket", side_effect=AssertionError("unexpected socket")):
                    app.open_repository()
                    deadline = time.monotonic() + 20
                    while app.busy and time.monotonic() < deadline:
                        window.update(); time.sleep(.01)
                    self.assertFalse(app.busy, "scan did not finish")
                    self.assertFalse(errors, errors)
                    app.pan_map(.15, -.15); self.assertEqual((app.pan_x, app.pan_y), (.15, -.15))
                    app.zoom_map(1.25); app.reset_zoom()
                    self.assertEqual((app.zoom, app.pan_x, app.pan_y), (1.0, 0.0, 0.0))
                    app.scope.set("Saved labels"); app.draw_graph(); self.assertIn("No labels are saved yet", app.map_status.get())
                    app.select("function:long.py:late")
                    app.scope.set("This file"); app.draw_graph(); self.assertIn("Showing this file", app.map_status.get())
                    app.calls.selection_set(0); app.show_call_details()
                    self.assertIn("could not tell", app.call_detail.get())
                    app.level.set("Technical"); app.level_changed(); self.assertIn("unscanned code", app.call_detail.get())
                    app.level.set("Getting started"); app.level_changed(); app.scope.set("Around selection")
                    self.assertEqual([q["kind"] for q in app.reading_questions], ["parameter", "return"])
                    app.next_reading_question(); self.assertEqual(app.reading_questions[app.reading_index]["kind"], "return")
                    question = app.reading_questions[app.reading_index]
                    answer = next(i for i, choice in enumerate(question["choices"]) if "Line 152:" in choice)
                    app.reading_choice.set(1 - answer); app.check_reading_answer()
                    self.assertIn("Try the other choice", app.reading_feedback.get("1.0", "end"))
                    app.reading_choice.set(answer); app.check_reading_answer()
                    self.assertIn("Supported by the scan", app.reading_feedback.get("1.0", "end"))
                    app.open_reading_source()
                    self.assertEqual(app.controller.selected, "function:long.py:late")
                    self.assertEqual(app.reading_questions[app.reading_index]["kind"], "return")
                    self.assertIn("return helper(value)", app.source_text.get("1.0", "end"))
                    self.assertTrue(app.source_text.tag_ranges("selected_source"))
                    # The whole file is shown while its bytes still match the scan, not just a captured excerpt.
                    self.assertIn("its bytes match this scan", app.source_text.get("1.0", "end"))
                    self.assertIn("    1: # padding", app.source_text.get("1.0", "end"))
                    self.assertIn("151: def late(value):", app.source_text.get("1.0", "end"))
                    self.assertTrue(app.source_text.tag_ranges("selected_source"))
                    late_call = next(node for node in app.controller.nodes.values() if node["kind"] == "callsite" and node.get("path") == "long.py")
                    late_edge = next(e for e in app.controller.data["edges"] if e["target"] == late_call["id"] and e["kind"] == "contains written call")
                    app.open_graph_connection(late_edge); app.open_connection_source()
                    self.assertEqual(app.controller.selected, late_call["id"])
                    self.assertIn("152:     return helper(value)", app.source_text.get("1.0", "end"))
                    long_source.write_text("# padding\n" * 150 + "def late(value):\n    return value + 1\n", encoding="utf-8")
                    app.show_selected()
                    # Changed bytes are never shown as scan evidence: the captured excerpt is used and the change is stated.
                    self.assertIn("changed on disk after the scan", app.source_text.get("1.0", "end"))
                    self.assertIn("captured context around the selected item", app.source_text.get("1.0", "end"))
                    self.assertIn("def late(value):", app.source_text.get("1.0", "end"))
                    self.assertNotIn("return value + 1", app.source_text.get("1.0", "end"))
                    app.select("function:m.py:start"); app.set_route_start()
                    app.select("function:m.py:helper"); app.build_item_path()
                    self.assertEqual(app.controller.route["status"], "unavailable")
                    app.include_inferred.set(True); app.build_item_path()
                    self.assertEqual(app.controller.route["goal"], "item")
                    self.assertEqual(app.controller.route["steps"][1]["via"]["evidence"], "inferred")
                    app.move_step(1); app.open_step_source()
                    self.assertEqual(app.controller.nodes[app.controller.selected]["kind"], "callsite")
                    self.assertIn("return helper(1)", app.source_text.get("1.0", "end"))
                    app.include_inferred.set(False)
                    app.select("function:m.py:helper"); app.label.set("My helper"); app.save_label()
                    caller_row = next(i for i, row in app.caller_ids.items() if row["caller_id"] == "function:m.py:start")
                    app.caller_list.selection_set(caller_row); app.show_caller()
                    self.assertIn("possible target: inferred", app.caller_text.get("1.0", "end"))
                    self.assertIn("return helper(1)", app.caller_text.get("1.0", "end"))
                    app.level.set("Technical"); app.level_changed()
                    self.assertIsNotNone(app.selected_caller())
                    self.assertIn("rebinding", app.caller_text.get("1.0", "end"))
                    app.route_from_caller()
                    self.assertEqual(app.controller.route["steps"][-1]["node_id"], "function:m.py:helper")
                    self.assertEqual(app.controller.route["steps"][-1]["via"]["evidence"], "inferred")
                    self.assertNotIn("different path", app.controller.route["message"])
                    app.open_caller_source()
                    self.assertIn("return helper(1)", app.source_text.get("1.0", "end"))
                    app.level.set("Getting started"); app.select("function:m.py:helper")
                    app.controller.data["unmatched_labels"].append({"node_id": "missing", "label": "Old label", "path": "old.py", "kind": "function"})
                    app.refresh_destinations()
                    missing_row = next(i for i, ident in app.label_inventory_ids.items() if ident is None)
                    app.label_inventory.selection_set(missing_row); app.open_inventory_label()
                    self.assertEqual(app.label_tools.index("current"), 1)
                    self.assertIn("unmatched", app.status.get())
                    app.controller.data["unmatched_labels"].clear(); app.refresh_destinations()
                    saved_row = next(i for i, ident in app.label_inventory_ids.items() if ident == "function:m.py:helper")
                    app.select("function:m.py:start"); app.label_inventory.selection_set(saved_row); app.open_inventory_label()
                    self.assertEqual(app.controller.selected, "function:m.py:helper")
                    app.scope.set("Saved labels"); app.draw_graph(); self.assertIn("Showing saved labels", app.map_status.get())
                    app.select("function:long.py:late"); app.label.set("Temporary label"); app.save_label()
                    app.remove_label(); self.assertEqual(app.status.get(), "Label removed from this item.")
                    self.assertNotIn("function:long.py:late", app.label_inventory_ids.values())
                    app.scope.set("Around selection")
                    app.select("function:m.py:start"); app.calls.selection_set(0); app.tabs.select(app.calls_tab); app.compare_call()
                    self.assertEqual(app.tabs.index("current"), 0)
                    self.assertIn("Possible target (inferred)", app.learn_text.get("1.0", "end"))
                    self.assertIn("1 written argument expression", app.learn_text.get("1.0", "end"))
                    self.assertIn("runtime values are unknown", app.learn_text.get("1.0", "end"))
                    self.assertIn("def helper", app.source_text.get("1.0", "end"))
                    self.assertTrue(app.source_text.tag_ranges("selected_source"))
                    graph_edges = app.canvas.find_withtag("graph_edge")
                    self.assertTrue(graph_edges); self.assertTrue(app.canvas.tag_bind(graph_edges[0], "<Button-1>"))
                    inferred = next(e for e in app.controller.data["edges"] if e["source"] == "function:m.py:start" and e["kind"] == "possible call")
                    edges = app.controller.data["edges"]; original_edges = edges[:]
                    try:
                        edges[:0] = [dict(inferred) for _ in range(205)]
                        app.open_graph_connection(inferred)
                        self.assertIs(app.connection_ids[app.connection_list.selection()[0]][0], inferred)
                    finally:
                        edges[:] = original_edges
                    self.assertEqual(app.tabs.index("current"), 2)
                    self.assertIn("possible link, not proof", app.connection_text.get("1.0", "end"))
                    app.level.set("Technical"); app.level_changed()
                    self.assertIn("rebinding", app.connection_text.get("1.0", "end"))
                    app.level.set("Getting started"); app.level_changed()
                    self.assertIn("possible link, not proof", app.connection_text.get("1.0", "end"))
                    app.open_connection_source()
                    self.assertEqual(app.tabs.index("current"), 1)
                    self.assertEqual(app.controller.nodes[app.controller.selected]["kind"], "callsite")
                    self.assertIn("return helper(1)", app.source_text.get("1.0", "end"))
                    self.assertTrue(app.source_text.tag_ranges("selected_source"))
                    app.select("function:m.py:start")
                    item = next(i for i, (edge, _, _) in app.connection_ids.items() if edge["kind"] == "possible call")
                    app.connection_list.selection_set(item); app.show_connection()
                    self.assertIn("possible call (inferred)", app.connection_text.get("1.0", "end"))
                    self.assertIn("return helper", app.connection_text.get("1.0", "end"))
                    app.open_connection(); self.assertEqual(app.controller.selected, "function:m.py:helper")
                    app.answer_practice("ran"); self.assertIn("Try again", app.practice_text.get("1.0", "end"))
                    app.answer_practice("source"); self.assertIn("Supported by the scan", app.practice_text.get("1.0", "end"))
                    app.go_back(); self.assertEqual(app.controller.selected, "function:m.py:start")
                    app.goal.set("Explore written calls"); app.prefer_unreviewed.set(True)
                    with patch.object(app.controller, "guide", wraps=app.controller.guide) as guide:
                        app.build_guide()
                    self.assertTrue(guide.call_args.args[2])
                    self.assertGreater(app.steps.size(), 0)
                    app.show_inferred.set(False); app.draw_graph(); self.assertIn("Some path links are hidden", app.map_status.get())
                    app.move_step(1); app.move_step(1); self.assertEqual(app.controller.selected, "function:m.py:helper")
                    app.toggle_review(); self.assertIn("marked this item reviewed", app.review_note.cget("text"))
                    app.show_warnings(); self.assertIn("Scan warnings", app.learn_text.get("1.0", "end"))
                    with patch("verifier_lab.elucidary_desktop.sys.platform", "win32"):
                        app.configure_model(); self.assertIn("not available", app.status.get())
                    app.remember(); self.assertTrue(app.shelf_items)
                    app.open_repository(compare=True)
                    deadline = time.monotonic() + 20
                    while app.busy and time.monotonic() < deadline:
                        window.update(); time.sleep(.01)
                    self.assertFalse(app.busy); self.assertFalse(errors, errors)
                    self.assertEqual(app.tabs.index("current"), 7)
                    changed_row = next(i for i, item in app.change_ids.items() if item["node_id"] == "function:long.py:late")
                    app.change_list.selection_set(changed_row); app.show_change()
                    self.assertIn("return helper(value)", app.change_text.get("1.0", "end"))
                    self.assertIn("return value + 1", app.change_text.get("1.0", "end"))
                    app.open_changed_item(); self.assertEqual(app.controller.selected, "function:long.py:late")
                    # A failed in-flight rescan clears the stale snapshot, re-enables the controls,
                    # and does not queue a second scan when the user requests one while busy.
                    with patch.object(app.controller, "open", side_effect=RuntimeError("synthetic scan failure")) as opening:
                        app.open_repository(compare=True)
                        app.open_repository(compare=True)
                        deadline = time.monotonic() + 20
                        while app.busy and time.monotonic() < deadline:
                            window.update(); time.sleep(.01)
                        self.assertFalse(app.busy); self.assertEqual(opening.call_count, 1)
                    self.assertEqual(len(errors), 1); self.assertIn("synthetic scan failure", str(errors.pop()))
                    self.assertIsNone(app.controller.data); self.assertEqual(app.change_ids, {})
                    self.assertEqual(str(app.open_button.cget("state")), "normal")
                    app.open_repository()
                    deadline = time.monotonic() + 20
                    while app.busy and time.monotonic() < deadline:
                        window.update(); time.sleep(.01)
                    self.assertFalse(app.busy); self.assertFalse(errors, errors)
                    self.assertEqual(app.controller.nodes["function:m.py:helper"]["label"], "My helper")
                    app.select("function:m.py:helper")
                    self.assertTrue(any(row["caller_id"] == "function:m.py:start" for row in app.caller_ids.values()))
                    app.open_repository(compare=True)
                    deadline = time.monotonic() + 20
                    while app.busy and time.monotonic() < deadline:
                        window.update(); time.sleep(.01)
                    self.assertFalse(app.busy); self.assertFalse(errors, errors)
                    self.assertEqual(app.change_ids, {})
                    app.audience.set("Adult reviewed for a young learner"); app.audience_changed()
                    self.assertIsNone(app.controller.data); self.assertEqual(app.shelf_items, []); self.assertEqual(app.change_ids, {})
            finally:
                if app.busy:
                    self.fail("worker still running")
                service.close(); window.destroy()


if __name__ == "__main__":
    unittest.main()

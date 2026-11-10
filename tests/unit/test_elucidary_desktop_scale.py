"""Drive the real desktop widgets over a repository large enough that per-click full passes would show.

The window is created but never shown. The repository is synthetic and is only read.
"""
import socket
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from verifier_lab.elucidary_desktop import DesktopApp, DesktopController, format_coverage
from verifier_lab.ingestion.common import Limits
from verifier_lab.service import Service

FOLDERS, FILES_PER_FOLDER, FUNCTIONS_PER_FILE = 12, 25, 4


def build_repository(root):
    for folder in range(FOLDERS):
        directory = root / f"area{folder:02d}" / "inner"
        directory.mkdir(parents=True)
        for number in range(FILES_PER_FOLDER):
            following = (number + 1) % FILES_PER_FOLDER
            lines = [f"from area{folder:02d}.inner.m{following:02d} import f0 as next_f0"]
            for function in range(FUNCTIONS_PER_FILE):
                target = f"f{function + 1}(value)" if function + 1 < FUNCTIONS_PER_FILE else "next_f0(value)"
                lines += [f"def f{function}(value):", f"    token = 'needle-{folder}-{number}-{function}'", f"    return {target}"]
            (directory / f"m{number:02d}.py").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (root / "third_party").mkdir(); (root / "third_party" / "dep.py").write_text("def vendored(): pass\n", encoding="utf-8")
    (root / "area00" / "inner" / "huge_pb2.py").write_text("def generated(): pass\n", encoding="utf-8")
    (root / "notes.md").write_text("# notes\n", encoding="utf-8")


class DesktopScaleTests(unittest.TestCase):
    def setUp(self):
        import tkinter as tk
        try:
            self.window = tk.Tk(); self.window.withdraw()
        except tk.TclError as exc:
            self.skipTest(f"Tk display unavailable: {exc}")
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name) / "repo"; self.repo.mkdir()
        build_repository(self.repo)
        self.service = Service(Path(self.tmp.name) / "ws", limits=Limits(isolation="portable"))
        self.app = DesktopApp(self.window, DesktopController(self.service))
        self.errors = []
        self.app.notify_error = self.errors.append
        self.app.audience.set("Adult"); self.app.audience_changed(); self.app.path.set(str(self.repo))
        self.app.isolation.set("Trusted development: no Linux sandbox")
        self.addCleanup(self.finish)

    def finish(self):
        deadline = time.monotonic() + 30
        while self.app.busy and time.monotonic() < deadline:
            self.window.update(); time.sleep(.01)
        for callback in self.window.tk.splitlist(self.window.tk.call("after", "info")):
            self.window.after_cancel(callback)
        self.service.close(); self.window.destroy(); self.tmp.cleanup()

    def wait(self, seconds=60):
        deadline = time.monotonic() + seconds
        while self.app.busy and time.monotonic() < deadline:
            self.window.update(); time.sleep(.01)
        self.assertFalse(self.app.busy, "the background job did not finish")

    def open(self):
        with patch.object(socket, "socket", side_effect=AssertionError("unexpected socket")):
            self.app.open_repository(); self.wait()
        self.assertFalse(self.errors, self.errors)

    def top_rows(self):
        return [self.app.tree.item(row, "text") for row in self.app.tree.get_children("")]

    def test_browse_search_label_and_reopen_a_repository_of_a_thousand_functions(self):
        app = self.app
        reports = []
        original = app.report_progress
        app.report_progress = lambda *report: (reports.append(report), original(*report))[1]
        self.open()
        files = FOLDERS * FILES_PER_FOLDER
        self.assertTrue(any(stage == "Reading source" and total == files + 1 for stage, _, total, _ in reports))
        self.assertIn(f"Inspected {files} files of {files + 1} supported files found", app.status.get())
        self.assertEqual(sum(n["kind"] == "function" for n in app.controller.nodes.values()), files * FUNCTIONS_PER_FILE)
        # The browser starts with the repository and its top folders, not thousands of rows.
        self.assertEqual(self.top_rows(), ["repo"] + [f"area{i:02d}/" for i in range(FOLDERS)])
        self.assertEqual(len(app.tree_ids), 1)
        self.assertIn("items by folder", app.tree_note.get())
        # Coverage states what was passed over.
        coverage = app.coverage_text.get("1.0", "end")
        self.assertIn(f"Parsed {files} of {files + 1} supported source files", coverage)
        self.assertIn("Dependency, build-output or virtual-environment folders", coverage); self.assertIn("third_party", coverage)
        self.assertIn("Generated or minified files", coverage); self.assertIn("huge_pb2.py", coverage)
        self.assertIn(".md 1", coverage)
        self.assertIn("Relationships between repositories are not mapped", coverage)
        # Selecting a deep item opens its folders and file in the browser.
        deep = "function:area07/inner/m13.py:f2"
        app.select(deep)
        row = app.tree_rows[deep]
        self.assertEqual(app.tree.selection(), (row,))
        self.assertTrue(app.tree.item(app.tree_folder_rows["area07"], "open")); self.assertTrue(app.tree.item(app.tree_folder_rows["area07/inner"], "open"))
        self.assertEqual(app.tree.item(app.tree.parent(row), "text"), "m13.py")
        self.assertLess(len(app.tree_ids), 80)
        source = app.source_text.get("1.0", "end")
        self.assertIn("its bytes match this scan", source); self.assertIn("needle-7-13-2", source)
        self.assertTrue(app.source_text.tag_ranges("selected_source"))
        # A cross-file inferred call is followed from the last function of one file to the next file.
        app.select("function:area07/inner/m13.py:f3")
        app.calls.selection_set(0); app.show_call_details(); app.follow_call("target_id")
        self.assertEqual(app.controller.selected, "function:area07/inner/m14.py:f0")
        self.assertIn(app.controller.selected, app.tree_rows)
        callers = [row["caller_id"] for row in app.caller_ids.values()]
        self.assertEqual(callers, ["function:area07/inner/m13.py:f3"])
        app.caller_list.selection_set(next(iter(app.caller_ids))); app.route_from_caller()
        self.assertEqual(app.controller.route["status"], "ready")
        self.assertEqual(app.controller.route["steps"][-1]["via"]["evidence"], "inferred")
        # Finding by name switches the browser to a bounded match list and back.
        app.search.set("m13.py"); app.apply_search()
        self.assertIn("items match", app.tree_note.get())
        self.assertTrue(0 < len(app.tree.get_children("")) <= app.TREE_SEARCH_LIMIT)
        app.search.set("f"); app.apply_search()
        self.assertEqual(len(app.tree.get_children("")), app.TREE_SEARCH_LIMIT)
        self.assertIn("first 500 are shown", app.tree_note.get())
        app.search.set(""); app.apply_search()
        self.assertEqual(self.top_rows()[0], "repo"); self.assertIn(app.controller.selected, app.tree_rows)
        # A label is saved, shown in the browser row, and survives a rescan that reuses the cache.
        app.select(deep); app.label.set("Deep helper"); app.save_label()
        self.assertEqual(app.tree.item(app.tree_rows[deep], "text"), "Deep helper")
        app.scope.set("Saved labels"); app.draw_graph(); self.assertIn("Showing saved labels", app.map_status.get())
        app.scope.set("Repository overview"); app.draw_graph(); self.assertIn("Showing 80 of", app.map_status.get())
        app.scope.set("Around selection"); app.remember()
        (self.repo / "area07" / "inner" / "m13.py").write_text((self.repo / "area07" / "inner" / "m13.py").read_text(encoding="utf-8").replace("needle-7-13-2", "changed-token"), encoding="utf-8")
        app.show_selected()
        self.assertIn("changed on disk after the scan", app.source_text.get("1.0", "end"))
        app.open_repository(compare=True); self.wait()
        self.assertFalse(self.errors, self.errors)
        self.assertIn(f"Reused {files - 1} unchanged files from the local scan cache; parsed 1 now", app.coverage_text.get("1.0", "end"))
        self.assertEqual(app.controller.nodes[deep]["label"], "Deep helper"); self.assertTrue(app.controller.nodes[deep]["label_needs_review"])
        self.assertEqual([change["node_id"] for change in app.change_ids.values() if change["status"] == "changed"],
                         ["file:area07/inner/m13.py", deep])
        app.clear_cache(); self.assertIn("0 file records", app.coverage_text.get("1.0", "end"))

    def test_source_search_runs_in_the_background_and_opens_the_matching_line(self):
        app = self.app
        self.open()
        app.source_query.set("x"); app.search_source(); self.assertIn("at least two", app.match_summary.get())
        app.source_query.set("NEEDLE-3-4-1"); app.search_source(); self.wait()
        self.assertFalse(self.errors, self.errors)
        self.assertEqual(len(app.match_ids), 1)
        self.assertIn("1 matching lines", app.match_summary.get())
        app.match_list.selection_set(next(iter(app.match_ids))); app.open_match()
        self.assertEqual(app.controller.selected, "function:area03/inner/m04.py:f1")
        self.assertEqual(app.tabs.index("current"), 1)
        highlighted = app.source_text.get(*app.source_text.tag_ranges("selected_source"))
        self.assertIn("needle-3-4-1", highlighted)
        app.source_query.set("needle-"); app.search_source(); self.wait()
        self.assertEqual(len(app.match_ids), 200); self.assertIn("reached its limit", app.match_summary.get())

    def test_scan_can_be_stopped_and_selection_stays_quick(self):
        app = self.app
        app.open_repository(); app.cancel_job(); self.wait()
        self.assertFalse(self.errors, self.errors)
        self.assertIn("Stopped", app.status.get()); self.assertIsNone(app.controller.data)
        self.assertEqual(str(app.cancel_button.cget("state")), "disabled")
        self.open()
        targets = [f"function:area{folder:02d}/inner/m{number:02d}.py:f{(folder + number) % FUNCTIONS_PER_FILE}"
                   for folder in range(FOLDERS) for number in range(0, FILES_PER_FILE_STEP * 5, FILES_PER_FILE_STEP)]
        started = time.perf_counter()
        for ident in targets:
            app.select(ident); self.window.update_idletasks()
        per_item = (time.perf_counter() - started) / len(targets)
        self.assertFalse(self.errors, self.errors)
        # Each selection redraws the map, source, links, callers and practice; it must not walk the repository.
        self.assertLess(per_item, 0.35, f"{per_item:.3f}s per selection")
        started = time.perf_counter()
        for _ in range(40): app.pan_map(.01, 0)
        self.assertLess((time.perf_counter() - started) / 40, 0.1)
        text = format_coverage(app.controller.data)
        self.assertIn("By top-level folder", text)


FILES_PER_FILE_STEP = 5

if __name__ == "__main__":
    unittest.main()

"""Run with a built desktop folder's own runtime: python.exe -I packaged_desktop_acceptance.py <report.json>

Drives the packaged application's real widgets (window hidden) through select, scan, browse, search,
follow callers, label, remember, restart, reopen and rescan, on a synthetic repository in every supported
language. The repository is only read: a marker file proves none of it ran, and any socket fails the run.
"""
import json
import socket
import sys
import tempfile
import time
import traceback
from pathlib import Path
from unittest.mock import patch

report_path = Path(sys.argv[1])
package = Path(sys.executable).resolve().parent.parent
report = {"package": str(package), "steps": []}


def step(name):
    report["steps"].append(name)


try:
    sys.path.insert(0, str(package / "app"))
    import tkinter as tk

    from verifier_lab.elucidary_desktop import DesktopApp, DesktopController
    from verifier_lab.ingestion.common import Limits
    from verifier_lab.service import Service

    build = json.loads((package / "BUILD.json").read_text(encoding="utf-8"))
    report["build"] = {key: build.get(key) for key in ("label", "source_commit", "source_tree_differed_from_commit", "application_sha256", "bundled_parsers")}
    app_dir = package / "app" / "verifier_lab"
    for excluded in ("server.py", "cli.py", "web", "ingestion/file_exchange.py", "ingestion/graph_reply_schema.py",
                     "ingestion/windows_reply_guard.py", "ingestion/windows_acl_plan.py"):
        assert not (app_dir / excluded).exists(), excluded
    for required in ("repository_workers.py", "repository_cache.py", "repository_index.py", "repository_records.py"):
        assert (app_dir / required).is_file(), required
    step("package contents")

    with tempfile.TemporaryDirectory() as tmp, patch.object(socket, "socket", side_effect=AssertionError("unexpected socket")):
        root = Path(tmp) / "repo"; marker = Path(tmp) / "executed"; workspace = Path(tmp) / "workspace"
        files = {
            "app/main.py": f"from pathlib import Path\nPath({str(marker)!r}).touch()\nfrom app.lib import helper\nclass Runner:\n    def run(self):\n        return self.step() + helper(1)\n    def step(self):\n        return 1\n",
            "app/__init__.py": "", "app/lib.py": "def helper(value):\n    marker = 'needle'\n    return value\n",
            "web/tools.js": "function greet(name) { return name?.trim() ?? ''; }\nmodule.exports = { greet };\n",
            "web/main.js": "const tools = require('./tools');\nclass Page {\n  open() { return this.draw() + tools.greet('x'); }\n  draw() { return 1; }\n}\n",
            "web/view.tsx": "import { size } from './size';\nexport function View() { return <b>{size()}</b>; }\n", "web/size.ts": "export function size(): number { return 1; }\n",
            "native/core.cpp": '#include "core.hpp"\nint Core::total() { return part() + scale(2); }\nint Core::part() { return 1; }\n',
            "native/core.hpp": "struct Core { int total(); int part(); };\nint scale(int v);\n", "native/scale.c": "int scale(int v) { return v * 2; }\n",
            "native/kernel.cu": "int launch(int n) { return n; }\n",
            "tools/Tool.cs": "namespace Tools {\n  public class Tool {\n    public int Run() { return Step(); }\n    int Step() { return 1; }\n  }\n}\n",
            "crate/src/main.rs": "mod util;\nfn main() { util::go(); }\n", "crate/src/util.rs": "pub fn go() {}\n",
            "third_party/dep/dep.py": "def vendored(): pass\n", "app/api_pb2.py": "def generated(): pass\n", "docs/readme.md": "# readme\n",
        }
        for name, text in files.items():
            target = root / name; target.parent.mkdir(parents=True, exist_ok=True); target.write_text(text, encoding="utf-8")
        (root / "app" / "latin.py").write_bytes("def latin():\n    return 'caf\xe9'\n".encode("cp1252"))

        def session():
            service = Service(workspace, limits=Limits(isolation="portable"))
            window = tk.Tk(); window.withdraw()
            app = DesktopApp(window, DesktopController(service)); errors = []
            app.notify_error = errors.append
            return service, window, app, errors

        def wait(window, app, seconds=120):
            deadline = time.monotonic() + seconds
            while app.busy and time.monotonic() < deadline:
                window.update(); time.sleep(.01)
            assert not app.busy, "background job did not finish"

        def end(service, window):
            for callback in window.tk.splitlist(window.tk.call("after", "info")):
                window.after_cancel(callback)
            service.close(); window.destroy()

        # ---------------- first session: select, scan, browse, search, follow, label, remember
        service, window, app, errors = session()
        try:
            assert app.isolation_key() is None, "Windows must require an explicit trusted-repository choice"
            app.audience.set("Adult"); app.audience_changed(); app.path.set(str(root))
            app.open_repository(); assert not app.busy and "parsing option" in app.status.get()
            app.isolation.set("Trusted repositories only: no parser sandbox")
            app.open_repository(); app.cancel_job(); wait(window, app)
            assert not errors and app.controller.data is None and "Stopped" in app.status.get(), (errors, app.status.get())
            step("stop a scan")
            started = time.monotonic(); app.open_repository(); wait(window, app); report["first_scan_seconds"] = round(time.monotonic() - started, 2)
            assert not errors, errors
            data = app.controller.data; coverage = data["coverage"]
            report["by_language"] = coverage["by_language"]
            assert {language for language, row in coverage["by_language"].items() if row["parsed"]} == {"python", "javascript", "typescript", "tsx", "cpp", "c", "csharp", "rust"}, coverage["by_language"]
            assert not marker.exists(), "repository code ran"
            assert coverage["skipped"]["vendor_or_build_directory"]["examples"] == ["third_party"] and coverage["skipped"]["generated"]["examples"] == ["app/api_pb2.py"]
            assert coverage["skipped"]["encoding"]["examples"] == ["app/latin.py"] and coverage["unsupported_extensions"] == {".md": 1}
            text = app.coverage_text.get("1.0", "end")
            assert "Parsed 15 of 16 supported source files" in text and "Generated or minified files" in text, text[:400]
            step("scan every bundled language with coverage reported")
            edges = {(e["source"], e["target"]): e for e in data["edges"] if e["kind"] == "possible call"}
            expected = [("function:app/main.py:Runner.run", "function:app/main.py:Runner.step"), ("function:app/main.py:Runner.run", "function:app/lib.py:helper"),
                        ("function:web/main.js:Page.open", "function:web/main.js:Page.draw"), ("function:web/main.js:Page.open", "function:web/tools.js:greet"),
                        ("function:web/view.tsx:View", "function:web/size.ts:size"), ("function:native/core.cpp:Core::total", "function:native/core.cpp:Core::part"),
                        ("function:native/core.cpp:Core::total", "function:native/scale.c:scale"), ("function:tools/Tool.cs:Tools.Tool.Run", "function:tools/Tool.cs:Tools.Tool.Step"),
                        ("function:crate/src/main.rs:main", "function:crate/src/util.rs:go")]
            for pair in expected:
                assert pair in edges and edges[pair]["evidence"] == "inferred" and edges[pair]["basis"], pair
            assert all(e["evidence"] in ("static", "inferred") for e in data["edges"])
            step("cross-file inferred calls carry their rule in every language")
            assert [app.tree.item(row, "text") for row in app.tree.get_children("")] == ["repo", "app/", "crate/", "native/", "tools/", "web/"]
            helper = "function:app/lib.py:helper"
            app.select(helper)
            assert app.tree.selection() == (app.tree_rows[helper],)
            shown = app.source_text.get("1.0", "end")
            assert "its bytes match this scan" in shown and "marker = 'needle'" in shown and app.source_text.tag_ranges("selected_source")
            step("browse folders to a function and read its whole verified file")
            app.search.set("greet"); app.apply_search(); assert "function:web/tools.js:greet" in app.tree_rows; app.search.set(""); app.apply_search()
            app.source_query.set("needle"); app.search_source(); wait(window, app)
            assert [m["node_id"] for m in app.match_ids.values()] == [helper], app.match_summary.get()
            app.match_list.selection_set(next(iter(app.match_ids))); app.open_match(); assert app.tabs.index("current") == 1
            step("find by name and search source text")
            caller = next(item for item, row in app.caller_ids.items() if row["caller_id"] == "function:app/main.py:Runner.run")
            app.caller_list.selection_set(caller); app.show_caller()
            assert "possible target: inferred" in app.caller_text.get("1.0", "end")
            # Possible callers -> the written call in source -> Back -> the same caller -> its path in the map.
            app.open_caller_source()
            highlighted = app.source_text.get(*app.source_text.tag_ranges("selected_source"))
            assert "helper(1)" in highlighted and app.tabs.index("current") == 1 and app.controller.nodes[app.controller.selected]["kind"] == "callsite"
            app.go_back(); assert app.controller.selected == helper
            caller = next(item for item, row in app.caller_ids.items() if row["caller_id"] == "function:app/main.py:Runner.run")
            app.caller_list.selection_set(caller); app.route_from_caller()
            route = app.controller.route
            assert route["status"] == "ready" and route["steps"][-1]["node_id"] == helper and route["steps"][-1]["via"]["evidence"] == "inferred"
            assert "different path" not in route["message"] and app.tabs.index("current") == 5
            assert app.controller.selected == "function:app/main.py:Runner.run", "the path opens at its start"
            app.move_step(1); assert app.controller.selected == helper
            app.open_step_source(); assert "helper(1)" in app.source_text.get(*app.source_text.tag_ranges("selected_source"))
            app.select("function:app/main.py:Runner.run"); app.calls.selection_set(1); app.follow_call("target_id"); assert app.controller.selected == helper
            app.scope.set("Repository overview"); app.draw_graph(); app.scope.set("Around selection"); app.draw_graph()
            assert app.canvas.find_withtag("graph_node")
            step("follow possible callers into the map, source and back")
            app.select(helper); app.label.set("Shared helper"); app.save_label(); app.toggle_review(); app.remember()
            assert app.tree.item(app.tree_rows[helper], "text") == "Shared helper" and app.shelf_items
            step("label, review mark and remember the folder")
        finally:
            end(service, window)

        # ---------------- second session: a fresh start reopens the saved work
        service, window, app, errors = session()
        try:
            app.audience.set("Adult"); app.audience_changed(); app.isolation.set("Trusted repositories only: no parser sandbox")
            app.refresh_shelf(); assert [item["path"] for item in app.shelf_items] == [str(root.resolve())], app.shelf_items
            app.shelf_box.current(0); app.use_shelf(); assert app.path.get() == str(root.resolve())
            started = time.monotonic(); app.open_repository(); wait(window, app); report["reopen_seconds"] = round(time.monotonic() - started, 2)
            assert not errors, errors
            node = app.controller.nodes[helper]
            assert node["label"] == "Shared helper" and node["reviewed"] and not node["label_needs_review"]
            assert app.controller.data["coverage"]["cache"] == {"reused": 15, "parsed_now": 0}, app.controller.data["coverage"]["cache"]
            assert any(app.controller.nodes[ident]["label"] == "Shared helper" for ident in app.label_inventory_ids.values() if ident)
            step("restart: remembered folder, label, review mark and cached scan are all back")
            (root / "app" / "lib.py").write_text("def helper(value):\n    marker = 'needle'\n    return value + 1\n", encoding="utf-8")
            app.select(helper); assert "changed on disk after the scan" in app.source_text.get("1.0", "end")
            app.open_repository(compare=True); wait(window, app); assert not errors, errors
            assert app.controller.data["coverage"]["cache"] == {"reused": 14, "parsed_now": 1}
            assert app.tabs.index("current") == 7 and any(change["node_id"] == helper and change["status"] == "changed" for change in app.change_ids.values())
            assert app.controller.nodes[helper]["label_needs_review"] and app.controller.nodes[helper]["review_needs_refresh"]
            assert not marker.exists(), "repository code ran"
            step("rescan after an edit: one file re-read, change listed, label flagged for review")
            assert sys.platform != "win32" or (app.configure_model() is None and "not available" in app.status.get())
            step("local model stays unavailable on Windows")
        finally:
            end(service, window)
    report["status"] = "passed"
except Exception:
    report["status"] = "failed"
    report["error"] = traceback.format_exc()
report_path.write_text(json.dumps(report, indent=1), encoding="utf-8")
raise SystemExit(0 if report["status"] == "passed" else 1)

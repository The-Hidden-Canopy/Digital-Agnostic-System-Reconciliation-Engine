"""Run against a built desktop runtime using an explicitly trusted temporary fixture."""
import json
import socket
import sys
import tempfile
import traceback
from pathlib import Path
from unittest.mock import patch

report = Path(sys.argv[1])
try:
    sys.path.insert(0, str(Path(sys.executable).resolve().parent.parent / "app"))
    from verifier_lab.elucidary_desktop import DesktopController, graph_constellations, graph_view
    from verifier_lab.ingestion.common import Limits
    from verifier_lab.service import Service
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "repo"; root.mkdir()
        marker = Path(tmp) / "executed"
        (root / "lib.py").write_text("def helper(x): return x\n", encoding="utf-8")
        (root / "main.py").write_text(f"from pathlib import Path\nPath({str(marker)!r}).touch()\nfrom lib import helper\ndef start(): return helper(1)\n", encoding="utf-8")
        (root / "main.js").write_text("function js(a) { return a; }\n", encoding="utf-8")
        (root / "main.ts").write_text("function ts(a: number): number { return a; }\n", encoding="utf-8")
        (root / "main.cpp").write_text("int cpp(int a) { return a; }\n", encoding="utf-8")
        (root / "repeated.py").write_text("import x, x\n" + "# padding\n" * 150 + "def late():\n" + "    # body\n" * 40 + "    return 1\n", encoding="utf-8")
        service = Service(Path(tmp) / "workspace", limits=Limits(isolation="portable"))
        try:
            controller = DesktopController(service)
            with patch.object(socket, "socket", side_effect=AssertionError("unexpected socket")):
                scan = controller.open(str(root), "adult", "portable")
                assert scan["files_inspected"] == 6, (scan["files_inspected"], scan["warnings"])
                assert {n["id"] for n in scan["nodes"] if n.get("path") == "repeated.py" and n["kind"] == "dependency"} == {"import:repeated.py:1:x", "import:repeated.py:1:x#2"}
                late = next(n for n in scan["nodes"] if n["id"] == "function:repeated.py:late")
                file = next(n for n in scan["nodes"] if n["id"] == "file:repeated.py")
                assert any(line["line"] == late["line"] and line["text"] == "def late():" for line in file["source_context"])
                assert any(line["line"] == 193 and line["text"] == "    return 1" for line in file["source_context"])
                assert not marker.exists()
                controller.select("function:main.py:start")
                assert controller.path_between("function:main.py:start", "function:lib.py:helper", "beginner", False, False)["status"] == "unavailable"
                route = controller.path_between("function:main.py:start", "function:lib.py:helper", "beginner", True, False)
                assert route["status"] == "ready" and route["steps"][1]["via"]["evidence"] == "inferred"
                callers = service.repository_possible_callers(scan["repo_id"], "function:lib.py:helper", "beginner", 3, scan["scan_id"])
                caller = next(row for row in callers["rows"] if row["caller_id"] == "function:main.py:start")
                assert caller["path"][0]["evidence"] == "inferred" and caller["path"][0]["call_evidence"] == "static"
                assert caller["path"][0]["path"] == "main.py" and "helper(1)" in caller["path"][0]["snippet"]
                questions = service.repository_reading_questions(scan["repo_id"], "function:lib.py:helper", "beginner", scan["scan_id"])
                parameter = next(q for q in questions if q["kind"] == "parameter")
                answer = parameter["choices"].index("x")
                checked = service.repository_check_reading(scan["repo_id"], "function:lib.py:helper", "beginner", scan["scan_id"], "parameter", answer)
                assert checked["correct"] and checked["evidence"]["path"] == "lib.py"
                assert service.store.q1("SELECT COUNT(*) AS n FROM repository_progress")["n"] == 0
                controller.select("function:lib.py:helper"); controller.label("My helper"); controller.remember()
                nodes, edges = list(controller.nodes.values()), scan["edges"]
                selected, positions = graph_view(nodes, edges, "function:lib.py:helper", limit=40, focus_path="lib.py")
                assert "function:lib.py:helper" in selected and all(controller.nodes[i].get("path") == "lib.py" for i in selected)
                assert any(path == "lib.py" for path, _, _ in graph_constellations(nodes, selected, positions, "function:lib.py:helper"))
                selected, _ = graph_view(nodes, edges, "function:main.py:start", limit=40, labeled=True)
                assert "function:lib.py:helper" in selected
                controller.select("function:main.py:start")
                assert controller.compare(0, "beginner")["target"]["node_id"] == "function:lib.py:helper"
                assert controller.path_to_label("function:lib.py:helper", "beginner", True)["status"] == "ready"
                assert controller.open(str(root), "adult", "portable")["files_inspected"] == 6
                assert controller.nodes["function:lib.py:helper"]["label"] == "My helper"
                baseline = controller.data
                (root / "lib.py").write_text("def helper(x): return x + 1\n", encoding="utf-8")
                compared = controller.open(str(root), "adult", "portable", (baseline, "adult"))
                changed = next(item for item in compared["comparison"]["changes"] if item["node_id"] == "function:lib.py:helper")
                assert changed["status"] == "changed" and changed["label"] == "My helper"
                assert "return x + 1" in changed["after"]["excerpt"][0]["text"]
                assert controller.nodes["function:lib.py:helper"]["label_needs_review"]
                assert not marker.exists()
        finally:
            service.close()
    report.write_text(json.dumps({"status": "passed"}), encoding="utf-8")
except Exception:
    report.write_text(json.dumps({"status": "failed", "error": traceback.format_exc()}), encoding="utf-8")
    raise SystemExit(1)

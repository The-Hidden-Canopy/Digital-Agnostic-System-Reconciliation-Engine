"""Whole-repository scanning: languages, inference rules, coverage reporting, workers, cache and cancellation.

Every repository here is synthetic and is only read; none of its code is imported or run.
"""
import os
import subprocess
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path

from verifier_lab.ingestion.common import Limits
from verifier_lab.repository_cache import ParseCache
from verifier_lab.repository_index import RepositoryIndex
from verifier_lab.repository_learning import possible_callers
from verifier_lab.repository_map import ScanCancelled, ScanLimits, looks_generated, normalize_source, scan_repository
from verifier_lab.repository_workers import ParserWorker, WorkerFailure
from verifier_lab.service import Service, ServiceError

PORTABLE = Limits(isolation="portable")


def desktop(**changes):
    return replace(ScanLimits.desktop(), **changes)


def write(root, files):
    for name, text in files.items():
        target = Path(root) / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(text, bytes):
            target.write_bytes(text)
        else:
            target.write_text(text, encoding="utf-8")


def scan(root, **kwargs):
    kwargs.setdefault("scan_limits", desktop())
    return scan_repository(str(root), limits=PORTABLE, **kwargs)


def calls(data, source):
    return {(e["target"], e["evidence"]): e for e in data["edges"] if e["kind"] == "possible call" and e["source"] == source}


class LanguageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_c_sources_resolve_by_unique_name_and_name_pointer_parameters(self):
        write(self.root, {"util.c": '#include "util.h"\nint twice(int *value, const char name[4]) { return *value * 2; }\n',
                          "util.h": "int twice(int *value, const char name[4]);\n",
                          "main.c": '#include "util.h"\nint main(void) { int x = 2; return twice(&x, "abc"); }\n'})
        data = scan(self.root)
        self.assertEqual(data["coverage"]["by_language"]["c"]["parsed"], 2)
        edge = calls(data, "function:main.c:main")[("function:util.c:twice", "inferred")]
        self.assertIn("compilation and linking are not resolved", edge["basis"])
        twice = next(n for n in data["nodes"] if n["id"] == "function:util.c:twice")
        self.assertEqual([p["name"] for p in twice["shape"]["parameters"]], ["value", "name"])
        self.assertTrue(any(e["kind"] == "possible local module" and e["target"] == "file:util.h" for e in data["edges"]))

    def test_cpp_members_qualified_calls_and_include_suffix_keep_their_basis(self):
        write(self.root, {
            "include/engine/widget.hpp": "namespace ui {\nstruct Widget {\n  int size() { return 1; }\n  int area() { return size() * size(); }\n  int draw();\n};\nint helper(int v) { return v; }\n}\n",
            "src/widget.cpp": '#include "engine/widget.hpp"\nint ui::Widget::draw() { return area(); }\nint run() { return ui::helper(3); }\n',
        })
        data = scan(self.root)
        area = calls(data, "function:include/engine/widget.hpp:ui.Widget.area")
        self.assertIn(("function:include/engine/widget.hpp:ui.Widget.size", "inferred"), area)
        self.assertIn("same type in this file", next(iter(area.values()))["basis"])
        run = calls(data, "function:src/widget.cpp:run")
        self.assertIn("unique qualified name", run[("function:include/engine/widget.hpp:ui.helper", "inferred")]["basis"])
        include = next(e for e in data["edges"] if e["kind"] == "possible local module")
        self.assertEqual(include["target"], "file:include/engine/widget.hpp")
        self.assertIn("include directories were not read", include["basis"])
        # draw() is defined in another file than area(): no same-file member exists, so nothing is guessed.
        self.assertFalse(calls(data, "function:src/widget.cpp:ui::Widget::draw"))

    def test_csharp_methods_resolve_within_a_type_and_by_unique_type_name(self):
        write(self.root, {"Tools/Helper.cs": "namespace App.Tools;\npublic static class Helper {\n  public static int Twice(int v) => v * 2;\n}\n",
                          "Program.cs": "using App.Tools;\nnamespace App {\n  public class Program {\n    int Local(int v) { return v; }\n"
                                        "    public async System.Threading.Tasks.Task<int> Run(int v) { foreach (var x in new[]{1}) { } return this.Local(v) + Local(v) + Helper.Twice(v) + other.Twice(v); }\n  }\n}\n"})
        data = scan(self.root)
        self.assertEqual(data["coverage"]["by_language"]["csharp"]["parsed"], 2)
        run = "function:Program.cs:App.Program.Run"
        found = calls(data, run)
        self.assertIn(("function:Program.cs:App.Program.Local", "inferred"), found)
        twice = found[("function:Tools/Helper.cs:App.Tools.Helper.Twice", "inferred")]
        self.assertIn("may instead be a variable or property", twice["basis"])
        node = next(n for n in data["nodes"] if n["id"] == run)
        self.assertTrue(node["shape"]["async"])
        self.assertIn("loop", [f["kind"] for f in node["shape"]["features"]])
        self.assertEqual([c["resolution"] for c in node["call_sites"]], ["inferred", "inferred", "inferred", "unresolved"])
        self.assertTrue(any(n["kind"] == "dependency" and n["name"] == "App.Tools" for n in data["nodes"]))

    def test_rust_modules_and_impl_methods_follow_the_conventional_layout_only(self):
        write(self.root, {"crate/src/main.rs": "mod util;\nuse crate::util::helper;\nuse util::deep::far as away;\nfn main() { helper(1); util::other(2); away(3); external::thing(4); }\n",
                          "crate/src/util.rs": "pub mod deep;\npub fn helper(v: i32) -> i32 { v }\npub fn other(v: i32) -> i32 { if v > 0 { return v; } v }\n",
                          "crate/src/util/deep.rs": "pub fn far(v: i32) -> i32 { v }\n",
                          "crate/src/shape.rs": "pub struct Shape;\nimpl Shape {\n  pub fn new() -> Self { Self::make() }\n  fn make() -> Self { Shape }\n  pub fn area(&self) -> i32 { self.side() }\n  fn side(&self) -> i32 { 1 }\n}\n"})
        data = scan(self.root)
        main = calls(data, "function:crate/src/main.rs:main")
        self.assertEqual({target for target, _ in main}, {"function:crate/src/util.rs:helper", "function:crate/src/util.rs:other", "function:crate/src/util/deep.rs:far"})
        self.assertTrue(all("Cargo manifests" in edge["basis"] for edge in main.values()))
        self.assertIn(("function:crate/src/shape.rs:Shape.make", "inferred"), calls(data, "function:crate/src/shape.rs:Shape.new"))
        self.assertIn(("function:crate/src/shape.rs:Shape.side", "inferred"), calls(data, "function:crate/src/shape.rs:Shape.area"))
        other = next(n for n in data["nodes"] if n["id"] == "function:crate/src/util.rs:other")
        self.assertEqual(other["shape"]["return_detail"], "explicit_only_tail_expressions_excluded")
        unresolved = [c["written"] for c in next(n for n in data["nodes"] if n["id"] == "function:crate/src/main.rs:main")["call_sites"] if c["resolution"] == "unresolved"]
        self.assertEqual(unresolved, ["external::thing"])

    def test_javascript_class_methods_commonjs_and_current_syntax(self):
        write(self.root, {"lib/tools.js": "function greet(name) { return name?.trim() ?? ''; }\nconst twice = (v) => v * 2;\nmodule.exports = { greet, twice };\n",
                          "lib/single.js": "function only() { return 1; }\nmodule.exports = only;\n",
                          "app.js": "const tools = require('./lib/tools');\nconst { twice: double } = require('./lib/tools');\nconst only = require('./lib/single');\n"
                                    "class App {\n  #count = 0;\n  start() { return this.step() + tools.greet('x') + double(2) + only(); }\n  step() { return 1; }\n}\n",
                          "view.ts": "export class View {\n  render(): number { return this.measure(); }\n  private measure(): number { return 1; }\n}\n"})
        data = scan(self.root)
        self.assertFalse(data["warnings"], data["warnings"])
        start = calls(data, "function:app.js:App.start")
        self.assertEqual({target for target, _ in start}, {"function:app.js:App.step", "function:lib/tools.js:greet", "function:lib/tools.js:twice", "function:lib/single.js:only"})
        self.assertIn(("function:view.ts:View.measure", "inferred"), calls(data, "function:view.ts:View.render"))
        self.assertTrue(all(e["evidence"] == "inferred" and e["basis"] for e in start.values()))

    def test_python_methods_submodules_and_path_suffix_modules(self):
        write(self.root, {"src/pkg/__init__.py": "", "src/pkg/core.py": "def build():\n    return 1\n",
                          "src/pkg/service.py": "from pkg import core\nfrom pkg.core import build\nimport json\nimport helper\nclass Service:\n    def run(self):\n        return self.step() + core.build() + build() + json.dumps(1) + helper.go() + self.missing()\n    def step(self):\n        return 1\n",
                          "src/pkg/helper.py": "def go():\n    return 1\n",
                          "tools/json.py": "def dumps(value):\n    return ''\n"})
        data = scan(self.root)
        run = calls(data, "function:src/pkg/service.py:Service.run")
        self.assertEqual({target for target, _ in run}, {"function:src/pkg/service.py:Service.step", "function:src/pkg/core.py:build", "function:src/pkg/helper.py:go"})
        self.assertIn("same type in this file", run[("function:src/pkg/service.py:Service.step", "inferred")]["basis"])
        self.assertIn("import path used at run time was not checked", run[("function:src/pkg/core.py:build", "inferred")]["basis"])
        # A standard-library name is never matched to a same-named file elsewhere in the tree.
        self.assertFalse(any(e["kind"] == "possible local module" and e["target"] == "file:tools/json.py" for e in data["edges"]))
        node = next(n for n in data["nodes"] if n["id"] == "function:src/pkg/service.py:Service.run")
        self.assertEqual([c["written"] for c in node["call_sites"] if c["resolution"] == "unresolved"], ["json.dumps", "self.missing"])

    def test_local_names_argument_counts_and_file_local_functions_are_not_guessed_across_files(self):
        # Each case below was a wrong guess found by reading real ecosystem code: a call to a local lambda or
        # parameter matched to an unrelated function that merely had a unique name somewhere in the scan.
        write(self.root, {
            "render/window.cpp": "struct Rig {};\nRig step(Rig r, int input, float dt) { return r; }\nbool is(const int& e, const char* kind) { return kind != nullptr; }\n",
            "play/menu.cpp": "void adjust(float& volume) {\n    const auto step = [&](float& v, float lo, float hi, float by) { v += by; };\n    step(volume, 0.0F, 1.0F, 0.1F);\n}\n"
                             "bool surface(const char* part) {\n    const auto is = [&](const char* p) { return part == p; };\n    return is(\"water\");\n}\n",
            "a/one.cpp": "namespace { int helper(int v) { return v; } }\nstatic int twice(int v) { return v * 2; }\nint one() { return helper(1) + twice(2); }\n",
            "a/two.cpp": "int two() { return helper(2) + twice(3); }\nint three() { return one(1, 2, 3); }\n",
            "shadow.py": "def handler(value):\n    return value\ndef nested_choice():\n    def handler(value):\n        return -value\n    return handler(1)\n"
                         "def run(handler, other):\n    other = handler\n    return handler(1) + other(2)\ndef spread(*items):\n    return handler(*items)\n"
                         "def wrong_count():\n    return handler(1, 2, 3)\ndef right():\n    return handler(1)\n",
            "shadow.ts": "export function load(id: string, retries?: number): string { return id; }\n"
                         "export function a(load: (x: string) => string) { return load('x'); }\n"
                         "export function b() { const load = (x: string) => x; return load('x'); }\n"
                         "export function c() { return load('x', 1, 2); }\nexport function d() { return load('x'); }\n",
        })
        data = scan(self.root)
        by_id = {n["id"]: n for n in data["nodes"]}

        def only_call(ident):
            return [(c["resolution"], c["target_id"], c["unresolved_form"]) for c in by_id[ident]["call_sites"]]
        self.assertEqual(only_call("function:play/menu.cpp:adjust"), [("unresolved", None, "local")])
        self.assertEqual(only_call("function:play/menu.cpp:surface"), [("unresolved", None, "local")])
        # File-local definitions resolve inside their own file and are never offered to another file.
        self.assertEqual(only_call("function:a/one.cpp:one"), [("inferred", "function:a/one.cpp:helper", None), ("inferred", "function:a/one.cpp:twice", None)])
        self.assertTrue(by_id["function:a/one.cpp:helper"]["file_local"]); self.assertTrue(by_id["function:a/one.cpp:twice"]["file_local"])
        self.assertEqual(only_call("function:a/two.cpp:two"), [("unresolved", None, "name"), ("unresolved", None, "name")])
        self.assertEqual(only_call("function:a/two.cpp:three"), [("unresolved", None, "arity")])
        # Python scoping is lexical: the nested definition is the target, and parameters or locals are not the module function.
        self.assertEqual(only_call("function:shadow.py:nested_choice"), [("inferred", "function:shadow.py:nested_choice.handler", None)])
        self.assertIn("inside the enclosing function", by_id["function:shadow.py:nested_choice"]["call_sites"][0]["basis"])
        self.assertEqual(only_call("function:shadow.py:run"), [("unresolved", None, "local"), ("unresolved", None, "local")])
        self.assertEqual(only_call("function:shadow.py:spread"), [("inferred", "function:shadow.py:handler", None)])
        self.assertEqual(only_call("function:shadow.py:wrong_count"), [("unresolved", None, "arity")])
        self.assertEqual(only_call("function:shadow.py:right"), [("inferred", "function:shadow.py:handler", None)])
        self.assertEqual(only_call("function:shadow.ts:a"), [("unresolved", None, "local")])
        self.assertEqual(only_call("function:shadow.ts:b"), [("inferred", "function:shadow.ts:b.load", None)])
        self.assertEqual(only_call("function:shadow.ts:c"), [("unresolved", None, "arity")])
        self.assertEqual(only_call("function:shadow.ts:d"), [("inferred", "function:shadow.ts:load", None)])
        from verifier_lab.repository_learning import explain_unresolved_call
        self.assertIn("local variable", explain_unresolved_call({"unresolved_form": "local"}, "technical"))
        self.assertIn("does not fit its parameters", explain_unresolved_call({"unresolved_form": "arity"}, "explore"))
        self.assertIn("does not mean it has no target", explain_unresolved_call({"unresolved_form": "local"}, "beginner"))

    def test_method_and_default_argument_counts_do_not_reject_valid_calls(self):
        write(self.root, {
            "m.py": "import functools\nclass K:\n    def plain(self, a, b=1):\n        return a\n    @staticmethod\n    def fixed(a):\n        return a\n"
                    "    @functools.cache\n    def wrapped(self, a):\n        return a\n"
                    "    def use(self):\n        return self.plain(1) + self.plain(1, 2) + self.fixed(3) + self.wrapped(1, 2, 3) + self.plain()\n"
                    "def kw(a, *, key, other=None):\n    return a\ndef caller():\n    return kw(1, key=2) + kw(1)\n",
            "lib.hpp": "int scale(int value, int factor = 2);\n", "lib.cpp": '#include "lib.hpp"\nint scale(int value, int factor) { return value * factor; }\n',
            "use.cpp": '#include "lib.hpp"\nint use() { return scale(3); }\n',
            "s.rs": "pub struct S;\nimpl S {\n  pub fn area(&self, k: i32) -> i32 { k }\n  pub fn run(&self) -> i32 { self.area(1) + S::area(self, 2) + self.area(1, 2) }\n}\n",
        })
        data = scan(self.root)
        by_id = {n["id"]: n for n in data["nodes"]}
        self.assertEqual([(c["written"], c["resolution"], c["unresolved_form"]) for c in by_id["function:m.py:K.use"]["call_sites"]],
                         [("self.plain", "inferred", None), ("self.plain", "inferred", None), ("self.fixed", "inferred", None),
                          ("self.wrapped", "inferred", None), ("self.plain", "unresolved", "arity")])
        self.assertIsNone(by_id["function:m.py:K.wrapped"]["shape"]["arity"])
        self.assertEqual(by_id["function:m.py:K.plain"]["shape"]["arity"], {"min": 1, "max": 2, "self": False})
        self.assertEqual([(c["resolution"], c["unresolved_form"]) for c in by_id["function:m.py:caller"]["call_sites"]], [("inferred", None), ("unresolved", "arity")])
        # C++ defaults usually sit on the declaration, so too few written arguments is not a reason to refuse.
        self.assertEqual(by_id["function:use.cpp:use"]["call_sites"][0]["target_id"], "function:lib.cpp:scale")
        self.assertEqual([(c["written"], c["resolution"], c["unresolved_form"]) for c in by_id["function:s.rs:S.run"]["call_sites"]],
                         [("self.area", "inferred", None), ("S::area", "inferred", None), ("self.area", "unresolved", "arity")])

    def test_every_inferred_link_carries_a_basis_and_no_link_claims_execution(self):
        write(self.root, {"a.py": "from b import f\nclass K:\n    def m(self):\n        return self.n() + f()\n    def n(self):\n        return 1\n", "b.py": "def f():\n    return 1\n",
                          "c.cpp": "int g() { return 1; }\nint h() { return g(); }\n", "d.rs": "fn x() -> i32 { y() }\nfn y() -> i32 { 1 }\n"})
        data = scan(self.root)
        self.assertEqual({e["evidence"] for e in data["edges"]}, {"static", "inferred"})
        for edge in data["edges"]:
            if edge["kind"] in ("possible call", "possible target"):
                self.assertEqual(edge["evidence"], "inferred")
                self.assertTrue(edge["basis"], edge)
            if edge["kind"] in ("contains", "defines", "imports", "includes", "contains written call"):
                self.assertEqual(edge["evidence"], "static", edge)
        self.assertIn("No runtime calls were observed", data["scope"])


class CoverageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "repo"
        self.root.mkdir()

    def test_skipped_content_is_counted_with_reasons_not_silently_dropped(self):
        write(self.root, {
            "app/main.py": "def main():\n    return 1\n", "app/copy.py": "def main():\n    return 1\n",
            "node_modules/dep/index.js": "function hidden() {}\n", "third_party/lib/x.c": "int x(void) { return 0; }\n",
            "out-of-tree/CMakeCache.txt": "", "out-of-tree/gen.cpp": "int generated() { return 0; }\n",
            ".secret/key.py": "def secret(): pass\n", ".hidden.py": "x = 1\n",
            "app/api_pb2.py": "def generated(): pass\n", "app/bundle.min.js": "function a(){}\n",
            "app/header.py": "# @generated by tool\ndef made(): pass\n",
            "app/big.py": "# padding\n" * 30,
            "app/latin.py": "name = 'caf\xe9'\ndef latin():\n    return name\n".encode("cp1252"),
            "app/wide.py": "def wide():\n    return 1\n".encode("utf-16"),
            "app/blob.py": b"\x00\x01\x02binary\x00",
            "app/broken.py": "def broken(:\n",
            "docs/readme.md": "# hi\n", "docs/data.json": "{}", "scripts/run.ps1": "Write-Host hi\n", "scripts/go.sh": "echo hi\n",
        })
        target = Path(self.tmp.name) / "outside"
        target.mkdir(); (target / "linked.py").write_text("def linked(): pass\n", encoding="utf-8")
        junction = subprocess.run(["cmd", "/c", "mklink", "/J", str(self.root / "linkdir"), str(target)], capture_output=True) if os.name == "nt" else None
        if junction is None:
            os.symlink(target, self.root / "linkdir", target_is_directory=True)
        data = scan(self.root, scan_limits=desktop(max_file_bytes=200))
        cov, skipped = data["coverage"], data["coverage"]["skipped"]
        names = {n["name"] for n in data["nodes"]}
        for absent in ("hidden", "x", "generated", "secret", "linked", "made"):
            self.assertNotIn(absent, names)
        self.assertGreaterEqual(skipped["vendor_or_build_directory"]["count"], 3)
        self.assertIn("out-of-tree", skipped["vendor_or_build_directory"]["examples"])
        self.assertEqual(skipped["hidden_directory"]["examples"], [".secret"])
        self.assertEqual(skipped["hidden_file"]["count"], 1)
        if junction is None or junction.returncode == 0:
            self.assertEqual(skipped["linked_directory"]["examples"], ["linkdir"])
        self.assertEqual(sorted(skipped["generated"]["examples"]), ["app/api_pb2.py", "app/bundle.min.js", "app/header.py"])
        self.assertEqual(skipped["oversized"]["examples"], ["app/big.py"])
        self.assertEqual(skipped["binary"]["examples"], ["app/blob.py"])
        self.assertEqual(skipped["could_not_parse"]["examples"], ["app/broken.py"])
        self.assertEqual(sorted(skipped["encoding"]["examples"]), ["app/latin.py", "app/wide.py"])
        self.assertTrue({"latin", "wide", "main"} <= names)
        self.assertEqual(cov["unsupported_extensions"], {".md": 1, ".json": 1, ".ps1": 1, ".sh": 1})
        self.assertEqual(cov["duplicate_content"]["groups"], 1)
        self.assertEqual(cov["by_language"]["python"]["parsed"], 4)
        self.assertEqual(cov["by_language"]["python"]["not_parsed"], 5)  # two generated, oversized, binary, broken
        self.assertEqual(cov["by_language"]["javascript"], {"found": 1, "parsed": 0, "partial": 0, "not_parsed": 1})
        self.assertEqual(cov["files_listed_without_parsing"], 6)
        # Unparsed files stay visible as items, with the reason, and carry no invented content.
        big = next(n for n in data["nodes"] if n["id"] == "file:app/big.py")
        self.assertEqual(big["parse_status"], "oversized")
        self.assertIn("Listed but not parsed", big["parse_note"])
        latin = next(n for n in data["nodes"] if n["id"] == "file:app/latin.py")
        self.assertIn("Windows-1252", latin["parse_note"])
        self.assertTrue(any("broken.py" in w and "near line 1" in w for w in data["warnings"]))
        # The digest is of the file's own bytes even when the text had to be re-encoded for reading.
        import hashlib
        self.assertEqual(latin["sha256"], hashlib.sha256((self.root / "app/latin.py").read_bytes()).hexdigest())
        self.assertEqual(next(n for n in data["nodes"] if n["id"] == "function:app/latin.py:latin")["sha256"], latin["sha256"])

    def test_options_include_vendored_and_generated_content(self):
        write(self.root, {"third_party/lib/x.py": "def vendored(): pass\n", "api_pb2.py": "def generated(): pass\n", "main.py": "def main(): pass\n"})
        default = {n["name"] for n in scan(self.root)["nodes"] if n["kind"] == "function"}
        widened = {n["name"] for n in scan(self.root, scan_limits=ScanLimits.desktop(include_vendored=True, include_generated=True))["nodes"] if n["kind"] == "function"}
        self.assertEqual(default, {"main"})
        self.assertEqual(widened, {"main", "vendored", "generated"})

    def test_file_limit_reports_how_much_was_left_out_and_where(self):
        write(self.root, {f"{folder}/m{i}.py": f"def f{i}(): pass\n" for folder in ("alpha", "beta") for i in range(6)})
        data = scan(self.root, scan_limits=desktop(max_files=8))
        cov = data["coverage"]
        self.assertEqual((cov["supported_files_found"], cov["files_parsed"], cov["files_not_inspected_due_to_file_limit"]), (12, 8, 4))
        self.assertEqual({row["name"]: (row["found"], row["inspected"]) for row in cov["top_level"]}, {"alpha": (6, 6), "beta": (6, 2)})
        self.assertTrue(any("only the first 8 supported source files" in w for w in data["warnings"]))

    def test_links_between_nested_repositories_are_counted_and_stay_inferences(self):
        from verifier_lab.elucidary_desktop import format_coverage
        write(self.root, {"lib-repo/.git/HEAD": "ref: refs/heads/main\n", "lib-repo/canopy_lib/__init__.py": "",
                          "lib-repo/canopy_lib/tool.py": "def shared(value):\n    return value\n",
                          "app-repo/.git/HEAD": "ref: refs/heads/main\n",
                          "app-repo/app.py": "from canopy_lib.tool import shared\nimport requests\ndef run():\n    return shared(1) + requests.get('x')\n"})
        both = scan(self.root)
        self.assertEqual(both["coverage"]["nested_repositories"], ["app-repo", "lib-repo"])
        self.assertEqual(both["coverage"]["cross_repository"]["links"], 2)  # the module and the call
        edge = calls(both, "function:app-repo/app.py:run")[("function:lib-repo/canopy_lib/tool.py:shared", "inferred")]
        self.assertIn("import path used at run time was not checked", edge["basis"])
        text = format_coverage(both)
        self.assertIn("2 inferred links join files in different ones", text); self.assertIn("no package manifest", text)
        # Scanned alone, the application records the import and the call as written and suggests no target.
        alone = scan(self.root / "app-repo")
        self.assertFalse([e for e in alone["edges"] if e["evidence"] == "inferred"])
        run = next(n for n in alone["nodes"] if n["id"] == "function:app.py:run")
        self.assertEqual([c["resolution"] for c in run["call_sites"]], ["unresolved", "unresolved"])
        self.assertIn("not mapped from a single-repository scan", format_coverage(alone))

    def test_generated_and_text_classification(self):
        self.assertEqual(looks_generated("x.min.js", b"a"), "generated-file name")
        self.assertEqual(looks_generated("x.py", b"# DO NOT EDIT\n"), "generated-file header")
        self.assertEqual(looks_generated("x.js", b"a" * 30_000), "very long lines (minified or data)")
        self.assertIsNone(looks_generated("x.py", b"def f():\n    pass\n" * 2000))
        self.assertEqual(normalize_source(b"\x00\x00"), (None, "binary content"))
        self.assertEqual(normalize_source("def f(): pass\n".encode()), (b"def f(): pass\n", None))


class WorkerAndCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "repo"
        write(self.root, {f"pkg/m{i}.py": f"from pkg.m{(i + 1) % 12} import f{(i + 1) % 12}\ndef f{i}(v):\n    return f{(i + 1) % 12}(v)\n" for i in range(12)})
        write(self.root, {"pkg/__init__.py": "", "web/a.ts": "export function a(): number { return 1; }\n", "native/n.cpp": "int n() { return 0; }\n"})

    @staticmethod
    def stable(data):
        return ([{k: v for k, v in n.items()} for n in data["nodes"]], data["edges"], data["warnings"])

    def test_pooled_workers_and_one_process_per_file_agree_exactly(self):
        pooled = scan(self.root, scan_limits=desktop(workers=3))
        single = scan(self.root, scan_limits=desktop(workers=0))
        self.assertEqual(self.stable(pooled), self.stable(single))
        self.assertEqual(pooled["files_inspected"], 15)

    def test_worker_is_replaced_after_a_timeout_or_a_crash(self):
        worker = ParserWorker(PORTABLE)
        try:
            self.assertEqual(worker.parse("repository-python", b"def f(): pass\n", "a.py")["nodes"][0]["name"], "f")
            worker.limits = Limits(isolation="portable", wall_seconds=0.000001)
            with self.assertRaises(WorkerFailure) as failure:
                worker.parse("repository-python", b"def g(): pass\n" * 20000, "b.py")
            self.assertEqual(failure.exception.code, "wall_clock_timeout")
            worker.limits = PORTABLE
            self.assertEqual(worker.parse("repository-python", b"def h(): pass\n", "c.py")["nodes"][0]["name"], "h")
            worker.process.kill(); worker.process.wait()
            self.assertEqual(worker.parse("repository-python", b"def i(): pass\n", "d.py")["nodes"][0]["name"], "i")
            self.assertGreaterEqual(worker.restarts, 1)
        finally:
            worker.close()
        with self.assertRaises(ValueError):
            ParserWorker(Limits(isolation="required"))

    def test_rescan_reparses_only_changed_files_and_matches_a_fresh_scan(self):
        cache = ParseCache(Path(self.tmp.name) / "cache" / "scan-cache.db")
        try:
            first = scan(self.root, cache=cache)
            self.assertEqual(first["coverage"]["cache"], {"reused": 0, "parsed_now": 15})
            second = scan(self.root, cache=cache)
            self.assertEqual(second["coverage"]["cache"], {"reused": 15, "parsed_now": 0})
            self.assertEqual(self.stable(second), self.stable(first))
            (self.root / "pkg/m3.py").write_text("def f3(v):\n    return v + 1\n", encoding="utf-8")
            (self.root / "pkg/new.py").write_text("def added():\n    return 0\n", encoding="utf-8")
            (self.root / "pkg/m5.py").unlink()
            third = scan(self.root, cache=cache)
            self.assertEqual(third["coverage"]["cache"], {"reused": 13, "parsed_now": 2})
            self.assertEqual(self.stable(third), self.stable(scan(self.root)))
            self.assertGreater(cache.stats()["records"], 0)
            cache.clear()
            self.assertEqual(cache.stats()["records"], 0)
        finally:
            cache.close()

    def test_damaged_cache_is_discarded_and_the_scan_still_completes(self):
        location = Path(self.tmp.name) / "cache" / "scan-cache.db"
        location.parent.mkdir()
        location.write_bytes(b"this is not a database" * 100)
        cache = ParseCache(location)
        try:
            self.assertEqual(scan(self.root, cache=cache)["files_inspected"], 15)
            self.assertEqual(scan(self.root, cache=cache)["coverage"]["cache"]["reused"], 15)
        finally:
            cache.close()

    def test_cancelled_scan_publishes_nothing_and_keeps_the_previous_snapshot(self):
        service = Service(Path(self.tmp.name) / "ws", limits=PORTABLE)
        try:
            reports = []
            first = service.repository_map(str(self.root), "adult", scan_limits=desktop(), use_cache=True,
                                           progress=lambda *report: reports.append(report))
            self.assertTrue(any(stage == "Reading source" and total == 15 for stage, _, total, _ in reports))
            self.assertEqual(reports[-1][0], "Linking definitions and calls")
            seen = []
            with self.assertRaises(ServiceError) as stopped:
                service.repository_map(str(self.root), "adult", scan_limits=desktop(workers=1),
                                       should_cancel=lambda: seen.append(1) or len(seen) > 4)
            self.assertEqual(stopped.exception.code, "scan_cancelled")
            # The earlier scan is still the current one.
            self.assertTrue(service.repository_explain(first["repo_id"], "function:pkg/m1.py:f1", "overview", "beginner", first["scan_id"])["facts"])
            with self.assertRaises(ScanCancelled):
                scan(self.root, should_cancel=lambda: True)
        finally:
            service.close()

    def test_callers_come_from_every_inferred_call_not_only_the_first_thirty(self):
        body = "".join(f"def t{i}():\n    return {i}\n" for i in range(40)) + "def busy():\n" + "".join(f"    t{i}()\n" for i in range(40))
        write(self.root, {"many.py": body})
        data = scan(self.root)
        busy = next(n for n in data["nodes"] if n["id"] == "function:many.py:busy")
        self.assertEqual(len(busy["call_sites"]), 30); self.assertTrue(busy["call_sites_truncated"])
        index = RepositoryIndex(data["nodes"], data["edges"])
        rows = possible_callers(data["nodes"], "function:many.py:t39", "technical", 1, index=index)["rows"]
        self.assertEqual([(row["caller_id"], row["path"][0]["line"], row["path"][0]["evidence"]) for row in rows], [("function:many.py:busy", 121, "inferred")])
        # Without the index the bounded per-function list is all there is, and it says so.
        legacy = possible_callers(data["nodes"], "function:many.py:t39", "technical", 1)
        self.assertEqual(legacy["rows"], []); self.assertTrue(legacy["call_sites_truncated"])


class SourceAndSearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "repo"
        write(self.root, {"a/one.py": "import os\n\ndef alpha(value):\n    marker = 'needle'\n    return value\n",
                          "a/two.py": "def beta():\n    return 'NEEDLE in caps'\n", "b/three.cpp": "int gamma() { return 3; } // needle\n"})
        self.service = Service(Path(self.tmp.name) / "ws", limits=PORTABLE)
        self.addCleanup(self.service.close)
        self.scan = self.service.repository_map(str(self.root), "adult", scan_limits=desktop())
        self.repo, self.sid = self.scan["repo_id"], self.scan["scan_id"]

    def test_full_source_is_shown_only_while_bytes_match_the_scan(self):
        shown = self.service.repository_source(self.repo, "function:a/one.py:alpha", self.sid)
        self.assertEqual((shown["status"], shown["path"], shown["lines"][2]), ("current", "a/one.py", "def alpha(value):"))
        (self.root / "a/one.py").write_text("def alpha(value):\n    return 'changed'\n", encoding="utf-8")
        changed = self.service.repository_source(self.repo, "function:a/one.py:alpha", self.sid)
        self.assertEqual((changed["status"], changed["lines"]), ("changed", []))
        (self.root / "a/one.py").unlink()
        self.assertEqual(self.service.repository_source(self.repo, "file:a/one.py", self.sid)["status"], "unavailable")
        with self.assertRaises(ServiceError):
            self.service.repository_source(self.repo, "repo", self.sid)
        with self.assertRaises(ServiceError) as stale:
            self.service.repository_source(self.repo, "file:a/two.py", "not-the-scan")
        self.assertEqual(stale.exception.code, "stale_repository")

    def test_text_search_reports_matches_their_items_and_what_was_not_searched(self):
        result = self.service.repository_search_source(self.repo, "needle", self.sid)
        self.assertEqual([(m["path"], m["line"], m["node_id"]) for m in result["matches"]],
                         [("a/one.py", 4, "function:a/one.py:alpha"), ("a/two.py", 2, "function:a/two.py:beta"), ("b/three.cpp", 1, "function:b/three.cpp:gamma")])
        self.assertEqual((result["files_searched"], result["files_total"], result["stopped"]), (3, 3, None))
        self.assertIn("not a relationship", result["limitation"])
        (self.root / "a/two.py").write_text("def beta():\n    return 'needle moved'\n", encoding="utf-8")
        again = self.service.repository_search_source(self.repo, "needle", self.sid)
        self.assertEqual([m["path"] for m in again["matches"]], ["a/one.py", "b/three.cpp"])
        self.assertEqual(again["files_changed_since_scan"], 1)
        self.assertEqual(self.service.repository_search_source(self.repo, "needle", self.sid, limit=1)["stopped"], "result limit")
        self.assertEqual(self.service.repository_search_source(self.repo, "needle", self.sid, should_cancel=lambda: True)["stopped"], "cancelled")
        with self.assertRaises(ServiceError):
            self.service.repository_search_source(self.repo, "n", self.sid)


class IndexTests(unittest.TestCase):
    def test_index_answers_match_a_full_pass_over_the_edge_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(tmp, {"x/a.py": "from x.b import g\ndef f():\n    return g() + g()\n", "x/b.py": "def g():\n    return 1\n", "x/__init__.py": "", "top.py": "def t(): pass\n"})
            data = scan(tmp)
            index = RepositoryIndex(data["nodes"], data["edges"])
            for node in data["nodes"]:
                expected = [e for e in data["edges"] if e["source"] == node["id"] or e["target"] == node["id"]]
                self.assertEqual(index.incident(node["id"]), expected)
            self.assertEqual(index.folders()[""], (["x"], ["file:top.py"]))
            self.assertEqual(index.folders()["x"][1], ["file:x/__init__.py", "file:x/a.py", "file:x/b.py"])
            self.assertEqual(index.enclosing("x/a.py", 3), "function:x/a.py:f")
            self.assertEqual(index.enclosing("x/a.py", 1), "file:x/a.py")
            found, total = index.search("B.PY")
            self.assertEqual(total, len(found)); self.assertIn("function:x/b.py:g", found)
            self.assertEqual(index.search("")[1], 0)


if __name__ == "__main__":
    unittest.main()

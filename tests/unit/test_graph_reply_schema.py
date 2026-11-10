import copy
import unittest

from verifier_lab.ingestion.extract import ExtractionFailure
from verifier_lab.ingestion.graph_reply_schema import validate_graph_result
from verifier_lab.repository_map import parse_python
from verifier_lab.repository_javascript import parse_javascript
from verifier_lab.repository_trees import parse_source


SAMPLES = (
    ("repository-python", "fixture.py", "from math import sin as wave\nasync def f(x, *, y=1):\n    if x:\n        return wave(x)\n    return y\n"),
    ("repository-javascript", "fixture.js", "import {wave} from './wave.js';\nexport function f(x) { if (x) return wave(x); return 1; }\n"),
    ("repository-typescript", "fixture.ts", "import {wave} from './wave';\nexport function f(x: number): number { if (x) return wave(x); return 1; }\n"),
    ("repository-tsx", "fixture.tsx", "export function f(x: number) { return <span>{x}</span>; }\n"),
    ("repository-jsx", "fixture.jsx", "export function f(x) { return <span>{x}</span>; }\n"),
    ("repository-cpp", "fixture.cpp", '#include "wave.h"\nint f(int x) { if (x) return wave(x); return 1; }\n'),
)


def parse(fmt, path, data):
    if fmt == "repository-python": return parse_python(data, path)
    if fmt == "repository-javascript": return parse_javascript(data, path)
    return parse_source(data, path, fmt.removeprefix("repository-"))


class GraphReplySchemaTests(unittest.TestCase):
    def test_real_synthetic_parser_shapes_are_accepted(self):
        for fmt, path, text in SAMPLES:
            with self.subTest(fmt=fmt):
                data = text.encode("utf-8"); result = parse(fmt, path, data)
                self.assertNotIn("error", result)
                self.assertTrue(result["nodes"])
                self.assertIs(validate_graph_result(result, fmt, data, path), result)

    def test_rejects_forged_evidence_invalid_references_and_malformed_fields(self):
        fmt, path, text = SAMPLES[0]; data = text.encode("utf-8")
        original = parse(fmt, path, data)
        def function(result): return next(n for n in result["nodes"] if n["kind"] == "function")
        def tamper_path(r): function(r)["path"] = "other.py"
        def tamper_hash(r): function(r)["sha256"] = "0" * 64
        def tamper_snippet(r): function(r)["snippet"] = "forged source"
        def tamper_excerpt(r): function(r)["shape"]["evidence_lines"][0]["text"] = "forged"
        def tamper_runtime(r): r["edges"][0]["evidence"] = "observed"
        def tamper_edge(r): r["edges"][0]["target"] = "function:other.py:nope"
        def tamper_line(r): r["calls"][0]["line"] = True
        def tamper_call_hash(r): r["calls"][0]["line_sha256"] = "0" * 64
        def tamper_feature(r): function(r)["shape"]["features"][0]["kind"] = "executed"
        def tamper_export(r): r["top_level"]["f"] = ["missing"]
        def duplicate_node(r): r["nodes"].append(copy.deepcopy(r["nodes"][0]))
        def excess_nodes(r): r["nodes"] = [r["nodes"][0]] * 601
        def unknown_field(r): r["nodes"][0]["runtime_trace"] = "invented"
        for mutate in (tamper_path, tamper_hash, tamper_snippet, tamper_excerpt, tamper_runtime,
                       tamper_edge, tamper_line, tamper_call_hash, tamper_feature, tamper_export,
                       duplicate_node, excess_nodes, unknown_field):
            result = copy.deepcopy(original); mutate(result)
            with self.subTest(mutation=mutate.__name__), self.assertRaises(ExtractionFailure) as exc:
                validate_graph_result(result, fmt, data, path)
            self.assertEqual(exc.exception.code, "worker_protocol_error")

    def test_parser_failure_is_bounded_and_not_worker_authored_message(self):
        result = {"error": "invalid or overly complex Python source"}
        self.assertEqual(validate_graph_result(result, "repository-python", b"invalid", "x.py"), result)
        for error in ([], "untrusted message"):
            with self.subTest(error=error), self.assertRaises(ExtractionFailure):
                validate_graph_result({"error": error}, "repository-python", b"invalid", "x.py")

    def test_repeated_python_imports_have_distinct_stable_ids(self):
        data = b"import x, x\n"; result = parse_python(data, "x.py")
        self.assertEqual([n["id"] for n in result["nodes"]], ["import:x.py:1:x", "import:x.py:1:x#2"])
        self.assertIs(validate_graph_result(result, "repository-python", data, "x.py"), result)


if __name__ == "__main__":
    unittest.main()

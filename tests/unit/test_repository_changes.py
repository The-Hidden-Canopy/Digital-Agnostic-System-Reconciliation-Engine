import unittest
from unittest.mock import patch

from verifier_lab.repository_changes import compare_scans


def function(ident, fingerprint, line, text, label=""):
    return {"id": ident, "kind": "function", "name": ident, "path": "sample.py", "line": line,
            "fingerprint": fingerprint, "sha256": "source-digest", "snippet": text, "label": label}


class ScanComparisonTests(unittest.TestCase):
    def test_compares_fingerprints_and_locations_without_claiming_execution(self):
        old = {"location_id": "same", "repo_id": "project", "nodes": [function("changed", "a", 1, "def f():\n    return 1", "Mine"),
                                                    function("moved", "b", 4, "def g(): pass"),
                                                    function("removed", "c", 8, "def old(): pass")]}
        new = {"location_id": "same", "repo_id": "project", "nodes": [function("changed", "z", 1, "def f():\n    return 2", "Mine"),
                                                    function("moved", "b", 7, "def g(): pass"),
                                                    function("added", "d", 11, "def new(): pass")]}
        result = compare_scans(old, new)
        self.assertEqual(result["counts"], {"changed": 1, "added": 1, "removed": 1, "line moved": 1})
        changes = {item["node_id"]: item for item in result["changes"]}
        self.assertEqual(changes["changed"]["label"], "Mine")
        self.assertIn("return 1", changes["changed"]["before"]["excerpt"][1]["text"])
        self.assertIn("return 2", changes["changed"]["after"]["excerpt"][1]["text"])
        self.assertIsNone(changes["removed"]["after"])
        self.assertIn("not behavior", result["limitation"])
        self.assertIn("scan omissions", result["limitation"])
        self.assertIn("No runtime calls were observed", result["limitation"])

    def test_location_mismatch_is_rejected_and_results_are_bounded(self):
        old = {"location_id": "one", "repo_id": "project", "nodes": []}
        with self.assertRaises(ValueError): compare_scans(old, {"location_id": "two", "repo_id": "project", "nodes": []})
        with self.assertRaises(ValueError): compare_scans(old, {"location_id": "one", "repo_id": "other", "nodes": []})
        new = {"location_id": "one", "repo_id": "project", "nodes": [function(str(i), str(i), i + 1, "x" * 500) for i in range(5)]}
        with patch("verifier_lab.repository_changes.MAX_CHANGE_ITEMS", 2):
            result = compare_scans(old, new)
        self.assertEqual(result["counts"]["added"], 5); self.assertEqual(len(result["changes"]), 2)
        self.assertTrue(result["truncated"])
        self.assertEqual(len(result["changes"][0]["after"]["excerpt"][0]["text"]), 200)
        self.assertTrue(result["changes"][0]["after"]["excerpt"][0]["truncated"])


if __name__ == "__main__":
    unittest.main()

import copy
import json
import unittest

from verifier_lab.canonical import canonical_bytes, record_id
from verifier_lab.model import Activity, Artifact, Assumption, Bundle, Claim, Finding, Verdict
from verifier_lab.validation import parse_bundle


def sample() -> Bundle:
    a = Artifact.make("sha256:" + "a" * 64, "text/plain", 3, "artifacts/" + "a" * 64)
    asm = Assumption.make("fictional", "scope")
    c = Claim.make("fact.equals", "k", {"expected": {"kind": "integer", "value": "1"}}, [a.id], [asm.id])
    act = Activity.make("ingestion.extract", [a.id], [], [], "gen")
    return Bundle.build([a], [c], [act], [asm], [])


def codes(raw: dict) -> set[str]:
    return {d.code for d in parse_bundle(canonical_bytes(raw))[1]}


class BundleContract(unittest.TestCase):
    def setUp(self):
        self.good = sample().to_dict()

    def test_valid_roundtrip_and_identity(self):
        bundle, diags = parse_bundle(canonical_bytes(self.good))
        self.assertEqual(diags, [])
        self.assertEqual(bundle.bundle_id, sample().bundle_id)

    def test_record_order_does_not_change_bundle_identity(self):
        a1 = Artifact.make("sha256:" + "a" * 64, "text/plain", 3, "artifacts/a")
        a2 = Artifact.make("sha256:" + "b" * 64, "text/plain", 3, "artifacts/b")
        self.assertEqual(Bundle.build([a1, a2], [], [], [], []).bundle_id, Bundle.build([a2, a1], [], [], [], []).bundle_id)

    def test_changed_authoritative_data_changes_identity(self):
        other = Claim.make("fact.equals", "k", {"expected": {"kind": "integer", "value": "2"}}, [], [])
        self.assertNotEqual(sample().claims[0].id, other.id)

    def test_unknown_and_missing_top_level_fields(self):
        raw = copy.deepcopy(self.good)
        raw["extra"] = 1
        self.assertIn("unknown_field", codes(raw))
        del raw["extra"]
        del raw["claims"]
        self.assertIn("missing_field", codes(raw))

    def test_unsupported_version(self):
        raw = copy.deepcopy(self.good)
        raw["schema_version"] = "2"
        self.assertIn("unsupported_version", codes(raw))

    def test_dangling_reference(self):
        raw = copy.deepcopy(self.good)
        raw["claims"][0]["evidence_refs"] = ["artifact:sha256:" + "f" * 64]
        raw["claims"][0]["id"] = record_id("claim", {k: v for k, v in raw["claims"][0].items() if k != "id"})
        self.assertIn("dangling_reference", codes(raw))

    def test_duplicate_ids_and_id_mismatch(self):
        raw = copy.deepcopy(self.good)
        raw["artifacts"].append(copy.deepcopy(raw["artifacts"][0]))
        self.assertIn("duplicate_id", codes(raw))
        raw = copy.deepcopy(self.good)
        raw["claims"][0]["subject"] = "tampered"
        self.assertIn("id_mismatch", codes(raw))

    def test_bool_is_not_an_integer(self):
        raw = copy.deepcopy(self.good)
        raw["artifacts"][0]["size_bytes"] = True
        self.assertIn("bad_integer", codes(raw))

    def test_unsafe_paths(self):
        for p in ("/abs", "../x", "a/../b", "a\\b", "C:/x", "", "a//b"):
            raw = copy.deepcopy(self.good)
            raw["artifacts"][0]["relative_path"] = p
            self.assertIn("unsafe_path", codes(raw), p)

    def test_malformed_digest(self):
        raw = copy.deepcopy(self.good)
        raw["artifacts"][0]["digest"] = "sha256:XYZ"
        self.assertIn("malformed_digest", codes(raw))

    def test_float_and_duplicate_key_in_manifest(self):
        self.assertEqual({d.code for d in parse_bundle(b'{"schema_version": 1.0}')[1]}, {"float_forbidden"})
        self.assertEqual({d.code for d in parse_bundle(b'{"a":1,"a":2}')[1]}, {"duplicate_key"})

    def test_diagnostics_sorted_and_no_partial_bundle(self):
        raw = copy.deepcopy(self.good)
        raw["claims"][0]["predicate"] = ""
        raw["artifacts"][0]["media_type"] = ""
        bundle, diags = parse_bundle(canonical_bytes(raw))
        self.assertIsNone(bundle)
        self.assertEqual(diags, sorted(diags))

    def test_finding_identity(self):
        f = Finding.make("claim:sha256:" + "0" * 64, Verdict.SUPPORTED, "r", "1", [], [], "ok", {"w": 1})
        self.assertEqual(f.id, f.expected_id())
        with self.assertRaises(ValueError):
            Finding.make("c", "BOGUS", "r", "1", [], [], "x")


if __name__ == "__main__":
    unittest.main()

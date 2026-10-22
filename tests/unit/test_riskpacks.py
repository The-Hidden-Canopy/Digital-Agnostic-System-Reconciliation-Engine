import copy
import itertools
import tempfile
import unittest
from pathlib import Path

from verifier_lab import riskpacks as RP
from verifier_lab.canonical import canonical_bytes
from verifier_lab.rules import builtin_registry

KNOWN = builtin_registry().implemented_predicates()
T, F, U = RP.Tri.TRUE, RP.Tri.FALSE, RP.Tri.UNKNOWN
EQ = lambda f, v: {"op": "eq", "fact": f, "value": v}


def base_body(**over):
    body = {"schema": RP.PACK_SCHEMA, "pack_id": "p", "version": "1", "title": "t", "compatible_bundle_versions": ["1"],
            "applicability": EQ("k", True), "source_refs": [{"title": "synthetic"}],
            "obligations": [{"obligation_id": "O1", "claim_predicate": "fact.equals", "subject": "a",
                             "rule_ref": {"rule_id": "rule.fact_equals", "version": "1"}}]}
    body.update(over)
    return body


def pack_bytes(**over):
    return canonical_bytes(RP.seal(base_body(**over)))


class TruthTables(unittest.TestCase):
    def test_and_or_not_full_tables(self):
        and_t = {(T, T): T, (T, F): F, (T, U): U, (F, T): F, (F, F): F, (F, U): F, (U, T): U, (U, F): F, (U, U): U}
        or_t = {(T, T): T, (T, F): T, (T, U): T, (F, T): T, (F, F): F, (F, U): U, (U, T): T, (U, F): U, (U, U): U}
        for a, b in itertools.product((T, F, U), repeat=2):
            self.assertEqual(RP.tri_and(a, b), and_t[(a, b)], (a, b))
            self.assertEqual(RP.tri_or(a, b), or_t[(a, b)], (a, b))
        self.assertEqual([RP.tri_not(x) for x in (T, F, U)], [F, T, U])

    def test_unknown_is_not_false_and_absent_is_not_a_negative(self):
        self.assertEqual(RP.evaluate_predicate(EQ("x", True), {})[0], U)
        self.assertEqual(RP.evaluate_predicate({"op": "exists", "fact": "x"}, {})[0], U)
        self.assertEqual(RP.evaluate_predicate({"op": "exists", "fact": "x"}, {"x": None})[0], F)   # explicitly stated absent
        self.assertEqual(RP.evaluate_predicate({"op": "not", "arg": EQ("x", True)}, {})[0], U)
        self.assertEqual(RP.evaluate_predicate({"op": "and", "args": [EQ("x", True), EQ("y", 1)]}, {"y": 2})[0], F)

    def test_no_type_coercion(self):
        self.assertEqual(RP.evaluate_predicate(EQ("x", 1), {"x": True})[0], F)
        self.assertEqual(RP.evaluate_predicate(EQ("x", True), {"x": 1})[0], F)
        self.assertEqual(RP.evaluate_predicate(EQ("x", "1"), {"x": 1})[0], F)
        self.assertEqual(RP.evaluate_predicate({"op": "in", "fact": "x", "values": ["a", 2]}, {"x": 2})[0], T)

    def test_facts_used_are_recorded(self):
        p = {"op": "or", "args": [EQ("a", 1), {"op": "and", "args": [EQ("b", 2), EQ("c", 3)]}]}
        self.assertEqual(RP.evaluate_predicate(p, {"a": 0, "b": 2})[1], ["a", "b", "c"])


class PredicateGrammar(unittest.TestCase):
    def test_rejects_anything_outside_the_bounded_grammar(self):
        bad = [{"op": "regex", "fact": "x", "pattern": ".*"}, {"op": "eq", "fact": "x", "value": None},
               {"op": "eq", "fact": "x", "value": [1]}, {"op": "eq", "fact": "x", "value": 1, "eval": "1+1"},
               {"op": "in", "fact": "x", "values": []}, {"op": "and", "args": []}, {"op": "not"}, {"op": "eq", "value": 1},
               "x == 1", {"op": "eq", "fact": "", "value": 1}]
        for p in bad:
            with self.assertRaises(RP.PackError, msg=p):
                RP.validate_predicate(p)
        deep = EQ("x", 1)
        for _ in range(20):
            deep = {"op": "not", "arg": deep}
        with self.assertRaises(RP.PackError) as cm:
            RP.validate_predicate(deep)
        self.assertEqual(cm.exception.code, "predicate_too_deep")


class PackValidation(unittest.TestCase):
    def code(self, data):
        with self.assertRaises(RP.PackError) as cm:
            RP.load_pack(data, KNOWN)
        return cm.exception.code

    def test_valid_pack_loads_and_is_pinned_by_content_digest(self):
        pack = RP.load_pack(pack_bytes(), KNOWN)
        self.assertEqual(pack.profile_ref()["digest"], pack.content_digest)
        self.assertEqual(RP.load_pack(pack_bytes(), KNOWN).content_digest, pack.content_digest)

    def test_changed_content_at_the_same_version_is_detected(self):
        sealed = RP.seal(base_body())
        sealed["title"] = "sneaky edit"
        self.assertEqual(self.code(canonical_bytes(sealed)), "digest_mismatch")

    def test_unknown_and_executable_looking_fields_are_never_silently_ignored(self):
        for extra in ({"python_module": "os"}, {"script": "rm -rf /"}, {"eval": "1"}, {"url": "http://x"}):
            self.assertEqual(self.code(canonical_bytes(RP.seal({**base_body(), **extra}))), "unknown_field", extra)
        body = base_body()
        body["obligations"][0]["hook"] = "x"
        self.assertEqual(self.code(canonical_bytes(RP.seal(body))), "unknown_field")

    def test_rule_references_must_exist_and_match_the_claim_predicate(self):
        b = base_body()
        b["obligations"][0]["rule_ref"] = {"rule_id": "rule.nope", "version": "1"}
        self.assertEqual(self.code(canonical_bytes(RP.seal(b))), "unknown_rule")
        b = base_body()
        b["obligations"][0]["claim_predicate"] = "fact.present"
        self.assertEqual(self.code(canonical_bytes(RP.seal(b))), "rule_predicate_mismatch")

    def test_structure_errors(self):
        dup = base_body()
        dup["obligations"].append(copy.deepcopy(dup["obligations"][0]))
        cases = {"duplicate_obligation_id": dup, "unsupported_bundle_version": base_body(compatible_bundle_versions=["9"]),
                 "unsupported_schema": base_body(schema="other"), "bad_obligations": base_body(obligations=[]),
                 "bad_identity": base_body(pack_id="has space")}
        for code, body in cases.items():
            self.assertEqual(self.code(canonical_bytes(RP.seal(body))), code)
        self.assertEqual(self.code(b'{"a":1,"a":2}'), "malformed_pack")
        self.assertEqual(self.code(b"[]"), "malformed_pack")
        incomplete = RP.seal(base_body())
        del incomplete["title"]
        self.assertEqual(self.code(canonical_bytes(incomplete)), "missing_field")


class Catalog(unittest.TestCase):
    def cat(self, tmp=None):
        return RP.PackCatalog(Path(tmp) if tmp else None, KNOWN)

    def test_builtin_example_pack_is_available_and_optional(self):
        packs = self.cat().list()
        self.assertEqual([p.pack_id for p in packs], ["synthetic-upload-controls"])
        self.assertEqual(self.cat().skipped, [])

    def test_conflicting_content_under_an_existing_identity_is_rejected(self):
        c = self.cat()
        c.install(pack_bytes())
        c.install(pack_bytes())                                   # identical content: idempotent
        with self.assertRaises(RP.PackError) as cm:
            c.install(pack_bytes(title="different"))
        self.assertEqual(cm.exception.code, "conflicting_content")

    def test_install_persists_and_reloads_only_valid_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.cat(tmp).install(pack_bytes())
            (Path(tmp) / "garbage.json").write_text("{not json")
            self.assertEqual([p.pack_id for p in self.cat(tmp).list()], ["p", "synthetic-upload-controls"])

    def test_requires_resolution_and_cycles(self):
        c = self.cat()
        c.install(pack_bytes(pack_id="a", requires=["b"]))
        c.install(pack_bytes(pack_id="b", requires=["a"]))
        with self.assertRaises(RP.PackError) as cm:
            c.resolve(["a"])
        self.assertEqual(cm.exception.code, "cyclic_pack_dependency")
        c.install(pack_bytes(pack_id="c", requires=["d"]))
        c.install(pack_bytes(pack_id="d"))
        self.assertEqual([p.pack_id for p in c.resolve(["c"])], ["c", "d"])
        with self.assertRaises(RP.PackError):
            c.resolve(["missing"])

    def test_diff_is_machine_readable_and_a_patch_label_does_not_hide_changes(self):
        old = RP.load_pack(pack_bytes(), KNOWN)
        b = base_body(version="1.0.1")                           # "patch" release that changes requirements
        b["obligations"][0]["subject"] = "other"
        b["obligations"].append({"obligation_id": "O2", "claim_predicate": "fact.present", "subject": "z",
                                 "rule_ref": {"rule_id": "rule.fact_present", "version": "1"}})
        b["applicability"] = EQ("k", False)
        new = RP.load_pack(canonical_bytes(RP.seal(b)), KNOWN)
        d = RP.diff_packs(old, new)
        self.assertEqual((d["added_obligations"], d["removed_obligations"]), (["O2"], []))
        self.assertEqual(d["changed_obligations"], [{"obligation_id": "O1", "fields": ["subject"]}])
        self.assertTrue(d["applicability_changed"] and not d["identical"])
        self.assertTrue(RP.diff_packs(old, old)["identical"])


class Aggregation(unittest.TestCase):
    def test_conjunction_semantics(self):
        a = RP.aggregate
        self.assertEqual(a(["SUPPORTED", "SUPPORTED"]), "SUPPORTED")
        self.assertEqual(a(["SUPPORTED", "CONTRADICTED", "INSUFFICIENT_EVIDENCE"]), "CONTRADICTED")
        self.assertEqual(a(["SUPPORTED", "INSUFFICIENT_EVIDENCE"]), "INSUFFICIENT_EVIDENCE")       # unresolved branch blocks support
        self.assertEqual(a(["SUPPORTED", "HUMAN_REVIEW_REQUIRED"]), "HUMAN_REVIEW_REQUIRED")
        self.assertEqual(a(["INVALID_EVIDENCE", "INSUFFICIENT_EVIDENCE"]), "INVALID_EVIDENCE")
        self.assertEqual(a([]), "INSUFFICIENT_EVIDENCE")
        self.assertEqual(a(["NOT_APPLICABLE"]), "NOT_APPLICABLE")

    def test_context_facts_are_validated_and_digest_is_order_independent(self):
        self.assertEqual(RP.context_digest({"a": 1, "b": True}), RP.context_digest({"b": True, "a": 1}))
        for bad in ({"a": 1.5}, {"a": [1]}, {"": 1}, [], {"a": {"x": 1}}):
            with self.assertRaises(RP.PackError):
                RP.check_facts(bad)


if __name__ == "__main__":
    unittest.main()

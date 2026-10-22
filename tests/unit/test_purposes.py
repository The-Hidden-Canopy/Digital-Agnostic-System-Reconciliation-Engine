import copy
import json
import random
import unittest
from pathlib import Path

from tests.helpers import ROOT
from verifier_lab import purposes as PU
from verifier_lab.canonical import canonical_bytes, sha256_digest
from verifier_lab.ingestion.common import Limits
from verifier_lab.ingestion.extract import build_record, extract_local

FIX = ROOT / "fixtures" / "purposes"
CAT = PU.PurposeCatalog(None)
WEAK, STRONG = CAT.get("inspect-checkpoint"), CAT.get("resume-exactly")
BIND = {"entries_count": ["checkpoint.entries_count"], "allocator_next": ["checkpoint.allocator_next"],
        "random_cursor": ["checkpoint.random_cursor"], "operation_cursor": ["checkpoint.operation_cursor"],
        "semantics_version": ["checkpoint.semantics_version"]}


def source(name: str, data: bytes | None = None) -> dict:
    data = data if data is not None else (FIX / name).read_bytes()
    wire = extract_local("json", data, None, Limits())
    rec, _ = build_record(sha256_digest(data), data, wire, Limits(), {})
    return {"artifact_id": "artifact:" + sha256_digest(data)[7:], "name": name, "coverage": rec["coverage"]["summary"],
            "facts": rec["facts"], "blocked_keys": rec["blocked_keys"]}


def assess(profile, names, bindings=BIND):
    roles = {r["role"] for r in profile.roles}
    return PU.evaluate_purpose(profile, {k: v for k, v in bindings.items() if k in roles}, [source(n) for n in names])


def reasons(a):
    return {o["id"]: (o["verdict"], o["reason"]) for o in a["obligations"]}


class ProfileValidation(unittest.TestCase):
    def body(self, **over):
        b = {"schema": PU.SCHEMA, "profile_id": "p", "version": "1", "title": "t",
             "roles": [{"role": "a", "min": 1, "max": 1, "value_kind": "integer"}]}
        b.update(over)
        return b

    def code(self, body):
        with self.assertRaises(PU.PurposeError) as cm:
            PU.load_profile(canonical_bytes(PU.seal(body)))
        return cm.exception.code

    def test_builtin_profiles_load_and_are_pinned_by_digest(self):
        self.assertEqual({p.profile_id for p in CAT.list()}, {"inspect-checkpoint", "resume-exactly"})
        sealed = PU.seal(self.body())
        sealed["title"] = "edited"
        with self.assertRaises(PU.PurposeError) as cm:
            PU.load_profile(canonical_bytes(sealed))
        self.assertEqual(cm.exception.code, "digest_mismatch")

    def test_invalid_profiles_are_rejected_as_profile_errors_not_subject_failures(self):
        self.assertEqual(self.code(self.body(roles=[{"role": "a", "min": 3, "max": 1}])), "invalid_profile_cardinality")
        self.assertEqual(self.code(self.body(roles=[{"role": "a", "min": 1}, {"role": "a", "min": 1}])), "bad_role")
        self.assertEqual(self.code(self.body(roles=[{"role": "a", "min": 1, "optional": True}])), "bad_cardinality")
        self.assertEqual(self.code(self.body(roles=[{"role": "a", "value_kind": "complex"}])), "bad_value_kind")
        self.assertEqual(self.code(self.body(script="x")), "unknown_field")
        self.assertEqual(self.code(self.body(coherence=[{"id": "c", "type": "regex", "fact": "x"}])), "bad_coherence_rule")
        self.assertEqual(self.code(self.body(coherence=[{"id": "c", "type": "same_value", "fact": "x"}] * 2)), "duplicate_coherence_id")
        self.assertEqual(self.code(self.body(roles=[])), "bad_roles")
        cyc = {"target": "b", "history": "h", "stages": ["a", "b"], "requires": {"a": ["b"], "b": ["a"]}}
        self.assertEqual(self.code(self.body(lifecycle=cyc)), "cyclic_lifecycle")
        self.assertEqual(self.code(self.body(lifecycle={"target": "z", "history": "h", "stages": ["a"]})), "bad_lifecycle")

    def test_versions_are_immutable_and_newer_versions_are_reported_not_applied(self):
        cat = PU.PurposeCatalog(None)
        with self.assertRaises(PU.PurposeError) as cm:
            cat.install(canonical_bytes(PU.seal({**copy.deepcopy(STRONG.body), "title": "changed"})))
        self.assertEqual(cm.exception.code, "conflicting_content")
        cat.install(canonical_bytes(PU.seal({**copy.deepcopy(STRONG.body), "version": "1.1.0"})))
        self.assertEqual(cat.newer_than("resume-exactly", "1.0.0"), "1.1.0")
        self.assertEqual(cat.get("resume-exactly", "1.0.0").content_digest, STRONG.content_digest)     # old identity stays reproducible

    def test_bindings_are_validated_and_roles_are_never_guessed_from_names(self):
        for bad in ({"nope": ["x"]}, {"entries_count": []}, {"entries_count": "x"}, {"entries_count": ["a", "a"]}, []):
            with self.assertRaises(PU.PurposeError):
                PU.check_bindings(WEAK, bad)
        a = assess(WEAK, ["map.json"], {})                      # nothing bound: the file name "map.json" establishes nothing
        self.assertEqual(reasons(a)["role:entries_count"], ("INSUFFICIENT_EVIDENCE", "role_not_bound"))


class Strengthening(unittest.TestCase):
    def test_relation_is_mechanical_and_one_directional(self):
        self.assertEqual(PU.strengthens(STRONG, WEAK), (True, []))
        ok, why = PU.strengthens(WEAK, STRONG)
        self.assertFalse(ok)
        self.assertTrue(any("allocator_next" in w for w in why))

    def test_names_are_irrelevant(self):
        renamed = PU.load_profile(canonical_bytes(PU.seal({**copy.deepcopy(STRONG.body), "profile_id": "totally-different-name"})))
        self.assertTrue(PU.strengthens(renamed, WEAK)[0])
        looser = copy.deepcopy(STRONG.body)
        looser["roles"][1]["min"] = 0
        looser["roles"][1]["optional"] = True
        looser = PU.load_profile(canonical_bytes(PU.seal({**looser, "profile_id": "resume-exactly-lite"})))
        self.assertFalse(PU.strengthens(looser, STRONG)[0])


class Matrix(unittest.TestCase):
    """Weak and strong purposes against complete, incomplete, malformed, mixed-generation, invalidated and ambiguous packages."""

    def test_one_package_gets_different_justified_outcomes_for_two_purposes(self):
        names = ["map.json"]
        self.assertEqual(assess(WEAK, names)["overall"], "SUPPORTED")
        strong = assess(STRONG, names)
        self.assertEqual(strong["overall"], "INSUFFICIENT_EVIDENCE")
        r = reasons(strong)
        self.assertEqual(r["role:allocator_next"], ("INSUFFICIENT_EVIDENCE", "role_missing"))
        self.assertEqual(r["coherence:same_generation"][0], "SUPPORTED")                 # only the map supplies a role, and states g7
        self.assertEqual(r["lifecycle:prerequisites"], ("INSUFFICIENT_EVIDENCE", "prerequisite_missing"))
        self.assertTrue(any("allocator_next" in w for w in strong["would_resolve"]))     # says what could resolve it, without promising

    def test_complete_package_supports_both(self):
        for p in (WEAK, STRONG):
            a = assess(p, ["map.json", "counters.json", "history.json"])
            self.assertEqual(a["overall"], "SUPPORTED", reasons(a))
        self.assertEqual(reasons(assess(STRONG, ["map.json", "counters.json", "history.json"]))["lifecycle:current_validity"],
                         ("SUPPORTED", "no_invalidation_recorded"))

    def test_torn_generation_fails_despite_individually_valid_sources(self):
        a = assess(STRONG, ["map.json", "counters-g6.json", "history.json"])
        self.assertEqual(a["overall"], "CONTRADICTED")
        self.assertEqual(reasons(a)["coherence:same_generation"], ("CONTRADICTED", "sources_from_different_generations"))
        self.assertTrue(all(reasons(a)[f"role:{r}"][0] == "SUPPORTED" for r in ("allocator_next", "random_cursor", "entries_count")))   # each role is fine alone
        gens = {v["value"]["value"] for o in a["obligations"] if o["id"].startswith("coherence") for v in o["witness"]["values"]}
        self.assertEqual(gens, {"g6", "g7"})                                  # the witness cites both generations

    def test_wrong_type_is_invalid_not_missing(self):
        a = assess(STRONG, ["map.json", "counters-bad-type.json", "history.json"])
        self.assertEqual(reasons(a)["role:allocator_next"], ("INVALID_EVIDENCE", "role_value_invalid"))
        self.assertEqual(a["overall"], "INVALID_EVIDENCE")
        self.assertEqual(assess(WEAK, ["map.json", "counters-bad-type.json"])["overall"], "SUPPORTED")    # weak purpose never needed it

    def test_later_verified_invalidation_beats_an_older_success(self):
        a = assess(STRONG, ["map.json", "counters.json", "history-invalidated.json"])
        self.assertEqual(reasons(a)["lifecycle:current_validity"], ("CONTRADICTED", "invalidated_after_success"))
        self.assertEqual(a["overall"], "CONTRADICTED")

    def test_ambiguous_ordering_prefers_no_status_and_keeps_both_records(self):
        a = assess(STRONG, ["map.json", "counters.json", "history-other-clock.json"])
        o = next(x for x in a["obligations"] if x["id"] == "lifecycle:current_validity")
        self.assertEqual((o["verdict"], o["reason"]), ("INSUFFICIENT_EVIDENCE", "ordering_ambiguous"))
        self.assertTrue({"recoverable", "invalidated"} <= {e["stage"] for e in o["witness"]["events"]})   # competing records retained
        self.assertTrue(o["witness"]["competing"])

    def test_prerequisite_must_be_recorded_not_asserted(self):
        a = assess(STRONG, ["map.json", "counters.json", "history-no-persist.json"])
        o = next(x for x in a["obligations"] if x["id"] == "lifecycle:prerequisites")
        self.assertEqual((o["verdict"], o["reason"], o["witness"]["missing"]), ("INSUFFICIENT_EVIDENCE", "prerequisite_missing", ["persisted"]))
        boolean = b'{"checkpoint": {"persisted": true, "recoverable": true}, "history": []}'
        src = source("flag.json", boolean)
        out = PU.evaluate_purpose(STRONG, BIND, [source("map.json"), source("counters.json"), src])
        self.assertNotEqual(out["overall"], "SUPPORTED")                       # a boolean field is an assertion, not a record

    def test_duplicate_and_malformed_lifecycle_entries(self):
        dup = json.dumps({"history": [{"stage": "observed", "seq": 1, "clock": "c"}, {"stage": "persisted", "seq": 1, "clock": "c"}]}).encode()
        a = PU.evaluate_purpose(STRONG, BIND, [source("map.json"), source("counters.json"), source("d.json", dup)])
        self.assertEqual(reasons(a)["lifecycle:records"], ("INVALID_EVIDENCE", "duplicate_lifecycle_entry"))
        bad = json.dumps({"history": [{"stage": "observed", "seq": "one", "clock": "c"}]}).encode()
        a = PU.evaluate_purpose(STRONG, BIND, [source("map.json"), source("counters.json"), source("b.json", bad)])
        self.assertEqual(reasons(a)["lifecycle:records"], ("INVALID_EVIDENCE", "malformed_lifecycle_record"))

    def test_cardinality_ambiguity_and_unreadable_sources(self):
        two = json.dumps({"checkpoint": {"entries_count": 1, "alt": 2}}).encode()
        bind = {"entries_count": ["checkpoint.entries_count", "checkpoint.alt"]}
        a = PU.evaluate_purpose(WEAK, bind, [source("m.json", two)])
        self.assertEqual(reasons(a)["role:entries_count"], ("INVALID_EVIDENCE", "cardinality_exceeded"))
        dup = b'{"checkpoint": {"entries_count": 1, "entries_count": 2}}'
        a = PU.evaluate_purpose(WEAK, bind, [source("d.json", dup)])
        self.assertEqual(reasons(a)["role:entries_count"], ("INVALID_EVIDENCE", "role_key_ambiguous"))
        broken = source("x.json", b'{"checkpoint": ')
        a = PU.evaluate_purpose(WEAK, bind, [broken])
        self.assertEqual(reasons(a)["role:entries_count"], ("INSUFFICIENT_EVIDENCE", "role_missing_unreadable_sources_present"))

    def test_malformed_optional_role_blocks_support_and_absent_optional_is_not_invented(self):
        note = json.dumps({"checkpoint": {"entries_count": 3, "note": 7}}).encode()
        bind = {"entries_count": ["checkpoint.entries_count"], "note": ["checkpoint.note"]}
        a = PU.evaluate_purpose(WEAK, bind, [source("n.json", note)])
        self.assertEqual(reasons(a)["role:note"], ("INVALID_EVIDENCE", "role_value_invalid"))
        self.assertEqual(a["overall"], "INVALID_EVIDENCE")                    # optional does not mean unchecked
        a = assess(WEAK, ["map.json"])
        self.assertNotIn("role:note", reasons(a))                              # not supplied: not evaluated, not invented

    def test_scope_statement_names_what_was_not_verified(self):
        a = PU.evaluate_purpose(WEAK, {"entries_count": BIND["entries_count"]}, [source("map.json"), source("history.json")])
        self.assertEqual(a["scope"]["evaluated_sources"], ["map.json"])
        self.assertIn("history.json", a["scope"]["sources_outside_evaluation_scope"])
        self.assertIn("not verified", a["scope"]["statement"])

    def test_obligations_are_reported_in_a_stable_order(self):
        a = assess(STRONG, ["map.json", "counters.json", "history.json"])
        roles = [o["id"] for o in a["obligations"] if o["kind"] == "role"]
        self.assertEqual(roles, sorted(roles))


class Monotonicity(unittest.TestCase):
    def test_removing_evidence_never_creates_support_and_strong_implies_weak(self):
        full = {n: (FIX / n).read_bytes() for n in ("map.json", "counters.json", "history.json")}
        rng = random.Random(3)
        for _ in range(60):
            doc = {n: json.loads(b) for n, b in full.items()}
            drops = rng.sample([("map.json", "checkpoint", "entries_count"), ("counters.json", "checkpoint", "allocator_next"),
                                ("counters.json", "checkpoint", "random_cursor"), ("history.json", "history", None),
                                ("counters.json", "meta", "generation")], rng.randint(0, 3))
            for n, k1, k2 in drops:
                if k2 is None:
                    doc[n][k1] = []
                else:
                    doc[n][k1].pop(k2, None)
            srcs = [source(n, json.dumps(d).encode()) for n, d in doc.items()]
            strong = PU.evaluate_purpose(STRONG, BIND, srcs)["overall"]
            weak = PU.evaluate_purpose(WEAK, {"entries_count": BIND["entries_count"]}, srcs)["overall"]
            if strong == "SUPPORTED":
                self.assertEqual(weak, "SUPPORTED")                            # a mechanically stronger purpose implies the weaker one
                self.assertEqual(drops, [])                                    # and needs every required piece
            if drops:
                self.assertNotEqual(strong, "SUPPORTED", drops)


if __name__ == "__main__":
    unittest.main()

import json
import random
import unittest

from verifier_lab.canonical import canonical_bytes, sha256_digest
from verifier_lab.engine import CheckPlan, check_claims
from verifier_lab.ingestion.common import Limits
from verifier_lab.ingestion.extract import build_record, extract_local
from verifier_lab.model import Activity, Artifact, Bundle, Claim, Finding, Verdict
from verifier_lab.rules import (EXTRACT_OP, EXTRACTION_MEDIA, PROVENANCE_MEDIA, BaseRule, CheckContext,
                                RuleRegistry, builtin_registry)

L = Limits()
EQ = lambda k, v: {"expected": {"kind": k, "value": v}}


class Lab:
    """Tiny in-memory bundle: one origin doc, N evidence docs; claims built directly (no UI/service)."""

    def __init__(self, evidence: dict[str, tuple[str, bytes]], origin=("origin.json", b'{"x": 1}')):
        self.blobs, arts, acts = {}, {}, []
        self.src = {}

        def add(data, media):
            d = sha256_digest(data)
            a = Artifact.make(d, media, len(data), "artifacts/" + d[7:])
            self.blobs[d], arts[a.id] = data, a
            return a

        def ingest(fmt, data, dialect=None):
            wire = extract_local(fmt, data, dialect, L)
            rec, rend = build_record(sha256_digest(data), data, wire, L, {})
            src = add(data, "application/" + fmt)
            outs = [add(canonical_bytes(rec), EXTRACTION_MEDIA).id]
            acts.append(Activity.make(EXTRACT_OP, [src.id], outs, [], rec["manifest"]["id"]))
            return src

        self.origin = ingest("json", origin[1])
        self.evidence = {name: ingest(fmt, data) for name, (fmt, data) in evidence.items()}
        self.arts, self.acts = arts, acts
        prov = add(canonical_bytes({"origin_source_artifact_id": self.origin.id}), PROVENANCE_MEDIA)
        self.prov = prov

    def claim(self, predicate, subject, args, names=None):
        refs = [self.prov.id] + [self.evidence[n].id for n in (names or self.evidence)]
        return Claim.make(predicate, subject, args, refs, [])

    def verdicts(self, *claims, registry=None):
        reg = registry or builtin_registry()
        bundle = Bundle.build(self.arts.values(), claims, self.acts, [], [])
        ctx = CheckContext(bundle, lambda a: self.blobs[a.digest])
        run = check_claims(ctx, bundle.claims, CheckPlan.from_registry(reg), reg)
        return bundle, run


def one(lab, claim):
    _, run = lab.verdicts(claim)
    assert not run.errors, run.errors
    return run.findings[0]


class FactEquals(unittest.TestCase):
    def setUp(self):
        self.lab = Lab({"cfg": ("json", b'{"limit": 200, "name": "a", "ratio": 2.5, "flag": true}')})

    def test_supported_contradicted_with_exact_witness(self):
        f = one(self.lab, self.lab.claim("fact.equals", "limit", EQ("integer", "200")))
        self.assertEqual((f.verdict, f.reason_code), (Verdict.SUPPORTED, "value_equal"))
        f = one(self.lab, self.lab.claim("fact.equals", "limit", EQ("integer", "100")))
        self.assertEqual((f.verdict, f.reason_code), (Verdict.CONTRADICTED, "value_mismatch"))
        w = f.witness["observations"][0]
        self.assertEqual(self.lab.blobs[self.lab.arts[w["artifact_id"]].digest][w["byte_start"]:w["byte_end"]], b"200")

    def test_numeric_semantics_not_lexical(self):
        self.assertEqual(one(self.lab, self.lab.claim("fact.equals", "ratio", EQ("decimal", "2.50"))).verdict, Verdict.SUPPORTED)

    def test_missing_fact_is_insufficient_not_contradicted(self):
        f = one(self.lab, self.lab.claim("fact.equals", "nope", EQ("integer", "1")))
        self.assertEqual((f.verdict, f.reason_code), (Verdict.INSUFFICIENT_EVIDENCE, "fact_not_found"))

    def test_kind_mismatch_needs_human_review(self):
        f = one(self.lab, self.lab.claim("fact.equals", "limit", EQ("string", "200")))
        self.assertEqual((f.verdict, f.reason_code), (Verdict.HUMAN_REVIEW_REQUIRED, "kind_mismatch"))

    def test_malformed_arguments_are_invalid_evidence(self):
        for args in ({}, {"expected": 5}, {"expected": {"kind": "integer", "value": 5}}, {"expected": {"kind": "x", "value": "1"}},
                     {"expected": {"kind": "integer", "value": "1"}, "extra": 1}):
            f = one(self.lab, self.lab.claim("fact.equals", "limit", args))
            self.assertEqual((f.verdict, f.reason_code), (Verdict.INVALID_EVIDENCE, "malformed_claim_arguments"), args)

    def test_not_applicable_requires_explicit_decision_rationale_and_scope(self):
        ok = {**EQ("integer", "1"), "applicability": {"decision": "NOT_APPLICABLE", "rationale": "other product", "scope": "v2 only"}}
        self.assertEqual(one(self.lab, self.lab.claim("fact.equals", "limit", ok)).verdict, Verdict.NOT_APPLICABLE)
        for bad in ({"decision": "NOT_APPLICABLE"}, {"decision": "NOT_APPLICABLE", "rationale": " ", "scope": "x"}, {"decision": "MAYBE"}, "n/a"):
            f = one(self.lab, self.lab.claim("fact.equals", "limit", {**EQ("integer", "1"), "applicability": bad}))
            self.assertEqual(f.verdict, Verdict.INVALID_EVIDENCE, bad)   # unknown applicability never becomes NOT_APPLICABLE

    def test_assertion_cannot_be_its_own_evidence(self):
        lab = Lab({}, origin=("o.json", b'{"limit": 1}'))
        lab.evidence["self"] = lab.origin
        f = one(lab, lab.claim("fact.equals", "limit", EQ("integer", "1")))
        self.assertEqual((f.verdict, f.reason_code), (Verdict.INSUFFICIENT_EVIDENCE, "no_independent_evidence"))


class ConflictAndCoverage(unittest.TestCase):
    def test_conflicting_sources_need_human_review(self):
        lab = Lab({"a": ("json", b'{"k": 1}'), "b": ("json", b'{"k": 2}')})
        f = one(lab, lab.claim("fact.equals", "k", EQ("integer", "1")))
        self.assertEqual((f.verdict, f.reason_code), (Verdict.HUMAN_REVIEW_REQUIRED, "evidence_conflict"))
        f = one(lab, lab.claim("fact.equals", "k", EQ("integer", "3")))
        self.assertEqual(f.verdict, Verdict.CONTRADICTED)

    def test_duplicate_keys_in_evidence_are_ambiguous_not_last_wins(self):
        lab = Lab({"a": ("json", b'{"k": 1, "k": 2}')})
        f = one(lab, lab.claim("fact.equals", "k", EQ("integer", "2")))
        self.assertEqual((f.verdict, f.reason_code), (Verdict.HUMAN_REVIEW_REQUIRED, "evidence_key_ambiguous"))

    def test_corrupt_evidence_is_invalid_and_unreadable_sibling_blocks_support(self):
        lab = Lab({"bad": ("json", b'{"k": ')})
        self.assertEqual(one(lab, lab.claim("fact.equals", "k", EQ("integer", "1"))).verdict, Verdict.INVALID_EVIDENCE)
        lab = Lab({"ok": ("json", b'{"k": 1}'), "bad": ("json", b"{")})
        f = one(lab, lab.claim("fact.equals", "k", EQ("integer", "1")))
        self.assertEqual((f.verdict, f.reason_code), (Verdict.HUMAN_REVIEW_REQUIRED, "evidence_source_unreadable"))
        # a witnessed contradiction does not depend on the unreadable sibling
        self.assertEqual(one(lab, lab.claim("fact.equals", "k", EQ("integer", "9"))).verdict, Verdict.CONTRADICTED)

    def test_no_evidence_selected(self):
        lab = Lab({})
        f = one(lab, lab.claim("fact.equals", "k", EQ("integer", "1")))
        self.assertEqual(f.verdict, Verdict.INSUFFICIENT_EVIDENCE)


class OtherRules(unittest.TestCase):
    def setUp(self):
        self.lab = Lab({"cfg": ("json", b'{"n": 5, "s": "x"}')})

    def bound(self, op, b):
        return one(self.lab, self.lab.claim("fact.numeric_bound", "n", {"op": op, "bound": {"kind": "integer", "value": b}})).verdict

    def test_numeric_bound(self):
        self.assertEqual(self.bound("lte", "5"), Verdict.SUPPORTED)
        self.assertEqual(self.bound("lt", "5"), Verdict.CONTRADICTED)
        self.assertEqual(self.bound("gt", "4"), Verdict.SUPPORTED)
        f = one(self.lab, self.lab.claim("fact.numeric_bound", "s", {"op": "lt", "bound": {"kind": "integer", "value": "1"}}))
        self.assertEqual(f.verdict, Verdict.HUMAN_REVIEW_REQUIRED)
        f = one(self.lab, self.lab.claim("fact.numeric_bound", "n", {"op": "approx", "bound": {"kind": "integer", "value": "1"}}))
        self.assertEqual(f.verdict, Verdict.INVALID_EVIDENCE)

    def test_present(self):
        self.assertEqual(one(self.lab, self.lab.claim("fact.present", "s", {})).verdict, Verdict.SUPPORTED)
        self.assertEqual(one(self.lab, self.lab.claim("fact.present", "zzz", {})).verdict, Verdict.INSUFFICIENT_EVIDENCE)

    def test_unknown_predicate_is_visible_not_silent_support(self):
        f = one(self.lab, self.lab.claim("quantum.entangled", "n", {}))
        self.assertEqual((f.verdict, f.rule_id, f.reason_code), (Verdict.INSUFFICIENT_EVIDENCE, "dispatch.unsupported", "no_registered_rule"))


class Dispatch(unittest.TestCase):
    def setUp(self):
        self.lab = Lab({"cfg": ("json", b'{"n": 5}')})

    def test_duplicate_registration_fails_and_selection_is_explicit(self):
        reg = RuleRegistry()
        reg.register(builtin_registry().get("rule.fact_present", "1"))
        with self.assertRaises(ValueError):
            reg.register(builtin_registry().get("rule.fact_present", "1"))

    def test_rule_crash_and_foreign_finding_become_execution_errors(self):
        class Crash(BaseRule):
            rule_id, predicate = "rule.crash", "t.crash"
            def evaluate(self, ctx, claim):
                raise RuntimeError("boom")

        class Foreign(BaseRule):
            rule_id, predicate = "rule.foreign", "t.foreign"
            def evaluate(self, ctx, claim):
                return Finding.make("claim:sha256:" + "0" * 64, Verdict.SUPPORTED, self.rule_id, "1", [], [], "x")

        class Fabricated(BaseRule):
            rule_id, predicate = "rule.fab", "t.fab"
            def evaluate(self, ctx, claim):
                return Finding.make(claim.id, Verdict.SUPPORTED, self.rule_id, "1", ["artifact:sha256:" + "9" * 64], [], "x")

        reg = builtin_registry()
        for r in (Crash(), Foreign(), Fabricated()):
            reg.register(r)
        claims = [self.lab.claim(p, "n", {}) for p in ("t.crash", "t.foreign", "t.fab")] + [self.lab.claim("fact.present", "n", {})]
        _, run = self.lab.verdicts(*claims, registry=reg)
        self.assertEqual(sorted(e.code for _, e in run.errors), ["finding_claim_mismatch", "finding_unknown_evidence", "rule_exception"])
        self.assertEqual([f.verdict for f in run.findings], [Verdict.SUPPORTED])   # neighbouring work continues

    def test_cancellation_is_cooperative_and_reported(self):
        claims = [self.lab.claim("fact.present", "n", {}), self.lab.claim("fact.present", "m", {})]
        reg = builtin_registry()
        bundle = Bundle.build(self.lab.arts.values(), claims, self.lab.acts, [], [])
        ctx = CheckContext(bundle, lambda a: self.lab.blobs[a.digest])
        calls = []
        run = check_claims(ctx, bundle.claims, CheckPlan.from_registry(reg), reg, should_cancel=lambda: len(calls) >= 1,
                           on_progress=lambda d, t, c: calls.append(d))
        self.assertTrue(run.cancelled)
        self.assertEqual((run.completed_claims, run.total_claims), (1, 2))

    def test_result_bytes_independent_of_claim_order(self):
        claims = [self.lab.claim("fact.present", k, {}) for k in ("n", "a", "b", "c", "d")]
        outs = set()
        for seed in range(6):
            shuffled = claims[:]
            random.Random(seed).shuffle(shuffled)
            _, run = self.lab.verdicts(*shuffled)
            outs.add(canonical_bytes([f.to_dict() for f in run.findings]))
        self.assertEqual(len(outs), 1)


class AlwaysPassBaseline(unittest.TestCase):
    """Negative control: a defective always-SUPPORTED rule must be caught by the independent checker."""

    def test_defective_rule_is_detected(self):
        import tempfile
        from pathlib import Path
        from tests.helpers import TUTORIAL
        from verifier_lab.checker import verify_package
        from verifier_lab.service import Service

        class AlwaysSupported(BaseRule):
            rule_id, predicate, rule_version = "rule.fact_equals", "fact.equals", "1"
            def evaluate(self, ctx, claim):
                return Finding.make(claim.id, Verdict.SUPPORTED, self.rule_id, "1", [], [], "value_equal", {"observations": []})

        reg = RuleRegistry()
        reg.register(AlwaysSupported())
        with tempfile.TemporaryDirectory() as tmp:
            svc = Service(Path(tmp), registry=reg)
            try:
                ids = {}
                for n in ("product-spec.md", "service-config.json"):
                    r = svc.import_source((TUTORIAL / n).read_bytes(), n)
                    svc.wait_job(r["job_id"])
                    ids[n] = r["source"]["id"]
                c = next(c for c in svc.list_candidates(source_id=ids["product-spec.md"], limit=500)["items"]
                         if c["fact_key"] == "limits.max_upload_kb")          # truly CONTRADICTED by the config
                rev = svc.decide(c["id"], "APPROVE", c["etag"])["claim_revision_id"]
                started = svc.start_run([rev], [ids["service-config.json"]])
                self.assertEqual(svc.wait_job(started["job_id"])["state"], "SUCCEEDED")
                self.assertEqual(svc.get_run(started["run_id"])["findings"][0]["verdict"], "SUPPORTED")  # the lie
                res = verify_package(svc.export_run(started["run_id"]))
                self.assertEqual(res["status"], "REJECTED")
                self.assertTrue(any("re-derived CONTRADICTED" in f["detail"] for f in res["findings"]))
            finally:
                svc.close()


if __name__ == "__main__":
    unittest.main()

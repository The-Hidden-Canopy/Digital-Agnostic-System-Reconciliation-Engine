import json
import shutil
import threading
import unittest
import zipfile
from pathlib import Path

from tests.helpers import TUTORIAL, WorkspaceCase, verdict_of
from verifier_lab.canonical import canonical_bytes, record_id, sha256_digest
from verifier_lab.checker import verify_package
from verifier_lab.service import ServiceError

SPEC_KEYS = ["limits.max_upload_kb", "limits.retries", "timeout.seconds", "region", "audit.enabled",
             "sql.table.users.column.email.not_null"]
EXPECTED = {"limits.max_upload_kb": "CONTRADICTED", "limits.retries": "SUPPORTED", "timeout.seconds": "SUPPORTED",
            "region": "SUPPORTED", "audit.enabled": "INSUFFICIENT_EVIDENCE",
            "sql.table.users.column.email.not_null": "SUPPORTED"}   # fixtures/README.md


class TutorialWorkflow(WorkspaceCase):
    def setUp(self):
        super().setUp()
        self.s = self.tutorial()
        self.revs = {k: self.approve(self.s["spec"]["id"], k) for k in SPEC_KEYS}

    def full_run(self):
        return self.do_run(list(self.revs.values()), [self.s["cfg"]["id"], self.s["sql"]["id"]])

    def test_expected_verdicts_match_fixture_readme(self):
        run = self.full_run()
        self.assertEqual(run["job"]["state"], "SUCCEEDED")
        for key, verdict in EXPECTED.items():
            self.assertEqual(verdict_of(run, key), verdict, key)
        self.assertEqual(run["freshness"]["state"], "CURRENT")

    def test_every_finding_cites_exact_bytes_and_package_verifies(self):
        run = self.full_run()
        f = next(f for f in run["findings"] if f["claim"]["subject"] == "limits.max_upload_kb")
        ev = f["evidence_view"][0]
        self.assertEqual((ev["source_name"], ev["token"]), ("service-config.json", "200"))
        res = verify_package(self.svc.export_run(run["id"]))
        self.assertEqual(res["status"], "VERIFIED", [c for c in res["checks"] if not c["ok"]] + [x for x in res["findings"] if not x["accepted"]])
        self.assertEqual(len(res["findings"]), len(EXPECTED))
        self.assertTrue(res["limitations"])

    def test_pdf_evidence_conflicts_with_spec_and_is_partial(self):
        self.assertEqual(self.s["pdf"]["attempt"]["coverage"], "PARTIALLY_PARSED")
        run = self.do_run([self.revs["limits.max_upload_kb"]], [self.s["pdf"]["id"]])
        self.assertEqual(verdict_of(run, "limits.max_upload_kb"), "CONTRADICTED")
        res = verify_package(self.svc.export_run(run["id"]))
        self.assertEqual(res["status"], "VERIFIED")
        # three-way: spec says 100, config says 200 and the PDF says 150 -> conflict among evidence, not a silent winner
        run = self.do_run([self.revs["limits.max_upload_kb"]], [self.s["cfg"]["id"], self.s["pdf"]["id"]])
        self.assertEqual(verdict_of(run, "limits.max_upload_kb"), "CONTRADICTED")

    def test_run_is_deterministic_across_workspaces(self):
        a = self.full_run()
        other = self.new_service("ws2")
        s2 = self.tutorial(other)
        revs2 = [self.approve(s2["spec"]["id"], k, other) for k in SPEC_KEYS]
        b = self.do_run(revs2, [s2["cfg"]["id"], s2["sql"]["id"]], other)
        self.assertEqual(a["bundle_id"], b["bundle_id"])
        with zipfile.ZipFile(self.svc.export_run(a["id"])) as x, zipfile.ZipFile(other.export_run(b["id"])) as y:
            self.assertEqual(x.namelist(), y.namelist())
            for n in x.namelist():
                if n != "run_metadata.json":      # observation metadata (timings, timestamps) is not semantic
                    self.assertEqual(x.read(n), y.read(n), n)

    def test_same_bytes_reimport_is_deduplicated_and_idempotent_run(self):
        again = self.svc.import_source((TUTORIAL / "service-config.json").read_bytes(), "renamed.json")
        self.assertTrue(again["deduplicated"])
        self.assertIsNone(again["job_id"])
        started = self.svc.start_run(list(self.revs.values()), [self.s["cfg"]["id"]])
        key = "k1"
        r1 = self.svc.start_run([self.revs["region"]], [self.s["cfg"]["id"]], idempotency_key=key)
        r2 = self.svc.start_run([self.revs["region"]], [self.s["cfg"]["id"]], idempotency_key=key)
        self.assertEqual(r1, r2)
        with self.assertRaises(ServiceError) as cm:
            self.svc.start_run([self.revs["limits.retries"]], [self.s["cfg"]["id"]], idempotency_key=key)
        self.assertEqual(cm.exception.code, "idempotency_key_reuse")
        self.svc.wait_job(started["job_id"])


class Tamper(WorkspaceCase):
    """Independent checker must reject every tampered variant of a valid package (chapter 13 K)."""

    def setUp(self):
        super().setUp()
        s = self.tutorial()
        revs = [self.approve(s["spec"]["id"], k) for k in ("limits.max_upload_kb", "limits.retries", "audit.enabled")]
        run = self.do_run(revs, [s["cfg"]["id"]])
        self.zip = self.svc.export_run(run["id"])
        self.dir = self.tmp / "pkg"
        with zipfile.ZipFile(self.zip) as zf:
            zf.extractall(self.dir)

    def fresh(self) -> Path:
        d = self.tmp / "work"
        shutil.rmtree(d, ignore_errors=True)
        shutil.copytree(self.dir, d)
        return d

    def rehash_manifest(self, d: Path):
        """Attacker fixes MANIFEST so only semantic checks can catch the edit."""
        files = []
        for p in sorted(d.rglob("*")):
            if p.is_file() and p.name != "MANIFEST.json":
                data = p.read_bytes()
                files.append({"path": p.relative_to(d).as_posix(), "sha256": sha256_digest(data), "size": len(data)})
        m = json.loads((d / "MANIFEST.json").read_bytes())
        m["files"] = files
        (d / "MANIFEST.json").write_bytes(canonical_bytes(m))

    def reject(self, d: Path, needle: str = ""):
        res = verify_package(d)
        self.assertEqual(res["status"], "REJECTED")
        text = json.dumps(res)
        self.assertIn(needle, text)
        return res

    def test_baseline_verified_for_dir_and_zip(self):
        self.assertEqual(verify_package(self.dir)["status"], "VERIFIED")
        self.assertEqual(verify_package(self.zip)["status"], "VERIFIED")

    def test_modified_artifact_byte(self):
        d = self.fresh()
        art = next(p for p in (d / "artifacts").iterdir() if p.read_bytes().startswith(b'{\n  "limits"'))
        art.write_bytes(art.read_bytes().replace(b"200", b"201"))
        self.reject(d, "bytes differ from manifest")

    def test_modified_artifact_with_rehashed_manifest_still_caught_by_bundle_digest(self):
        d = self.fresh()
        art = next(p for p in (d / "artifacts").iterdir() if p.read_bytes().startswith(b'{\n  "limits"'))
        art.write_bytes(art.read_bytes().replace(b"200", b"201"))
        self.rehash_manifest(d)
        self.reject(d, "identity/path/digest")

    def test_missing_artifact_and_unlisted_file(self):
        d = self.fresh()
        next((d / "artifacts").iterdir()).unlink()
        self.reject(d, "missing")
        d = self.fresh()
        (d / "evil.sh").write_text("rm -rf /")
        self.reject(d, "evil.sh")

    def test_flipped_verdict_with_recomputed_ids_is_rejected(self):
        d = self.fresh()
        res = json.loads((d / "result.json").read_bytes())
        for f in res["findings"]:
            if f["verdict"] == "CONTRADICTED":
                f["verdict"], f["reason_code"] = "SUPPORTED", "value_equal"
                body = {k: v for k, v in f.items() if k != "id"}
                f["id"] = record_id("finding", body)             # self-consistent forgery
        (d / "result.json").write_bytes(canonical_bytes(res))
        self.rehash_manifest(d)
        r = self.reject(d, "re-derived CONTRADICTED")
        self.assertEqual(sum(1 for f in r["findings"] if not f["accepted"]), 1)

    def test_forged_witness_pointing_at_other_bytes(self):
        d = self.fresh()
        res = json.loads((d / "result.json").read_bytes())
        for f in res["findings"]:
            if f["verdict"] == "SUPPORTED":
                f["witness"]["observations"][0]["byte_start"] += 1
                f["id"] = record_id("finding", {k: v for k, v in f.items() if k != "id"})
        (d / "result.json").write_bytes(canonical_bytes(res))
        self.rehash_manifest(d)
        self.reject(d, "witness observations differ")

    def test_dropped_finding_and_claim_without_finding(self):
        d = self.fresh()
        res = json.loads((d / "result.json").read_bytes())
        res["findings"] = res["findings"][:-1]
        (d / "result.json").write_bytes(canonical_bytes(res))
        self.rehash_manifest(d)
        self.reject(d, "every_claim_has_a_finding")

    def test_cancelled_or_partial_package_is_not_accepted_as_complete(self):
        d = self.fresh()
        res = json.loads((d / "result.json").read_bytes())
        res["completion"]["claims_completed"] -= 1
        (d / "result.json").write_bytes(canonical_bytes(res))
        self.rehash_manifest(d)
        self.reject(d, "run_complete")

    def test_missing_review_decision_in_provenance(self):
        d = self.fresh()
        for p in (d / "artifacts").iterdir():
            raw = p.read_bytes()
            if b"claim-provenance" in raw:
                prov = json.loads(raw)
                prov["decision"]["action"] = "REJECT"
                new = canonical_bytes(prov)
                p.unlink()
                (d / "artifacts" / sha256_digest(new)[7:]).write_bytes(new)
                break
        self.rehash_manifest(d)
        self.reject(d)           # digest no longer matches its bundle record

    def test_malformed_packages(self):
        d = self.fresh()
        (d / "bundle.json").write_bytes(b'{"a":1,"a":2}')
        self.reject(d)
        bad = self.tmp / "bad.zip"
        bad.write_bytes(self.zip.read_bytes()[:-40])
        self.reject(bad, "not a directory or valid zip")
        with zipfile.ZipFile(self.tmp / "trav.zip", "w") as zf:
            zf.writestr("../evil", b"x")
        self.reject(self.tmp / "trav.zip", "unsafe or duplicate archive member")

    def test_checker_never_imports_producer_modules(self):
        import ast
        src = (Path(__file__).resolve().parents[2] / "src/verifier_lab/checker.py").read_text()
        for node in ast.walk(ast.parse(src)):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""] if isinstance(node, ast.ImportFrom) else []
            self.assertFalse(any(n.startswith("verifier_lab") or (isinstance(node, ast.ImportFrom) and node.level) for n in names), names)


class StalenessAndRevisions(WorkspaceCase):
    def setUp(self):
        super().setUp()
        self.s = self.tutorial()
        self.rev = self.approve(self.s["spec"]["id"], "limits.retries")
        self.run1 = self.do_run([self.rev], [self.s["cfg"]["id"]])
        self.assertEqual(self.run1["freshness"]["state"], "CURRENT")

    def causes(self):
        return {c["kind"] for c in self.svc.get_run(self.run1["id"])["freshness"]["causes"]}

    def test_edited_claim_marks_run_stale_but_keeps_history(self):
        c = self.cand(self.s["spec"]["id"], "limits.retries")
        out = self.svc.decide(c["id"], "APPROVE_WITH_EDIT", c["etag"], reason="tighten",
                              edit={"arguments": {"expected": {"kind": "integer", "value": "4"}}})
        self.assertNotEqual(out["claim_revision_id"], self.rev)
        run = self.svc.get_run(self.run1["id"])
        self.assertEqual(run["freshness"]["state"], "STALE")
        self.assertIn("claim_revision_superseded", self.causes())
        self.assertEqual(run["findings"][0]["verdict"], "SUPPORTED")       # historical statement preserved
        with self.assertRaises(ServiceError) as cm:                        # stale revision cannot be re-run
            self.svc.start_run([self.rev], [self.s["cfg"]["id"]])
        self.assertEqual(cm.exception.code, "claim_revision_not_current")

    def test_rejecting_after_approval_retracts_claim(self):
        c = self.cand(self.s["spec"]["id"], "limits.retries")
        self.svc.decide(c["id"], "REJECT", self.svc.get_candidate(c["id"])["etag"], reason="wrong scope")
        self.assertIn("claim_revision_retracted", self.causes())

    def test_replacing_evidence_source_marks_dependent_run_stale(self):
        new = (TUTORIAL / "service-config.json").read_bytes().replace(b"200", b"300")
        res = self.svc.import_source(new, "service-config.json", replaces=self.s["cfg"]["id"])
        self.svc.wait_job(res["job_id"])
        self.assertEqual(self.svc.get_run(self.run1["id"])["freshness"]["state"], "STALE")
        self.assertIn("evidence_superseded", self.causes())
        with self.assertRaises(ServiceError) as cm:
            self.svc.start_run([self.rev], [self.s["cfg"]["id"]])
        self.assertEqual(cm.exception.code, "evidence_superseded")

    def test_reextracting_sql_with_new_dialect_creates_new_attempt_and_stales_candidates(self):
        sql = self.s["sql"]
        key = "sql.table.users.column.email.not_null"
        old = self.cand(sql["id"], key)
        res = self.svc.reextract(sql["id"], "sqlite")
        self.svc.wait_job(res["job_id"])
        self.assertNotEqual(res["attempt_id"], sql["attempt"]["id"])
        with self.assertRaises(ServiceError) as cm:
            self.svc.decide(old["id"], "APPROVE", old["etag"])
        self.assertEqual(cm.exception.code, "candidate_superseded")
        self.assertEqual(self.svc.get_source(sql["id"])["attempts"], 2)


class ReviewSemantics(WorkspaceCase):
    def setUp(self):
        super().setUp()
        self.s = self.tutorial()
        self.c = self.cand(self.s["spec"]["id"], "limits.max_upload_kb")

    def test_if_match_is_required_and_stale_etag_is_rejected_without_overwrite(self):
        with self.assertRaises(ServiceError) as cm:
            self.svc.decide(self.c["id"], "DEFER", None)
        self.assertEqual(cm.exception.status, 428)
        self.svc.decide(self.c["id"], "DEFER", self.c["etag"])
        with self.assertRaises(ServiceError) as cm:
            self.svc.decide(self.c["id"], "APPROVE", self.c["etag"])   # stale
        self.assertEqual((cm.exception.status, cm.exception.details["etag"]), (412, '"c1"'))
        self.assertEqual(self.svc.get_candidate(self.c["id"])["status"], "DEFERRED")

    def test_reasons_required_and_unchanged_edits_refused(self):
        with self.assertRaises(ServiceError) as cm:
            self.svc.decide(self.c["id"], "REJECT", self.c["etag"])
        self.assertEqual(cm.exception.code, "reason_required")
        with self.assertRaises(ServiceError) as cm:
            self.svc.decide(self.c["id"], "APPROVE_WITH_EDIT", self.c["etag"], reason="x",
                            edit={"arguments": {"expected": {"kind": "integer", "value": "100"}}})
        self.assertEqual(cm.exception.code, "edit_without_change")

    def test_bad_edits(self):
        for edit in ({"predicate": "nope"}, {"subject": " "}, {"arguments": {"expected": 3}}, {"arguments": []},
                     {"arguments": {"expected": {"kind": "integer", "value": "1"}, "applicability": {"decision": "NOT_APPLICABLE"}}},
                     {"bogus": 1}):
            with self.assertRaises(ServiceError, msg=edit):
                self.svc.decide(self.c["id"], "APPROVE_WITH_EDIT", self.c["etag"], reason="r", edit=edit)
        self.assertEqual(self.svc.get_candidate(self.c["id"])["decisions"], [])

    def test_original_source_untouched_by_edits_and_history_is_append_only(self):
        before = self.svc.source_text(self.s["spec"]["id"])["text"]
        out = self.svc.decide(self.c["id"], "APPROVE_WITH_EDIT", self.c["etag"], reason="units are kB",
                              edit={"subject": "limits.max_upload_kb", "arguments": {"expected": {"kind": "integer", "value": "128"}}})
        self.assertEqual(self.svc.source_text(self.s["spec"]["id"])["text"], before)
        out = self.svc.decide(self.c["id"], "REJECT", out["etag"], reason="changed my mind")
        self.assertEqual([d["action"] for d in out["decisions"]], ["APPROVE_WITH_EDIT", "REJECT"])
        self.assertEqual(out["predicate"]["arguments"]["expected"]["value"], "100")      # candidate itself never mutates

    def test_decision_idempotency(self):
        a = self.svc.decide(self.c["id"], "DEFER", self.c["etag"], idempotency_key="same")
        b = self.svc.decide(self.c["id"], "DEFER", self.c["etag"], idempotency_key="same")
        self.assertEqual(a, b)
        self.assertEqual(len(self.svc.get_candidate(self.c["id"])["decisions"]), 1)

    def test_reconciliation_proposes_but_never_merges(self):
        rel = {r["candidate_id"]: r for r in self.svc.related(self.c["id"])}
        kinds = {r["relation"] for r in rel.values()}
        self.assertEqual(kinds, {"POSSIBLE_CONTRADICTION"})       # cfg says 200, pdf says 150
        self.assertEqual(len(rel), 2)
        dup = self.cand(self.s["spec"]["id"], "limits.retries")
        self.assertIn("DUPLICATE", {r["relation"] for r in self.svc.related(dup["id"])})
        self.assertEqual(self.svc.list_candidates(limit=500)["total"], 7 + 4 + 5 + 2)   # nothing deleted

    def test_not_applicable_requires_scoped_rationale_and_is_reported(self):
        out = self.svc.decide(self.c["id"], "APPROVE_WITH_EDIT", self.c["etag"], reason="other tenant",
                              edit={"arguments": {"expected": {"kind": "integer", "value": "100"},
                                                  "applicability": {"decision": "NOT_APPLICABLE", "rationale": "legacy tier", "scope": "tier-0 only"}}})
        run = self.do_run([out["claim_revision_id"]], [self.s["cfg"]["id"]])
        self.assertEqual(verdict_of(run, "limits.max_upload_kb"), "NOT_APPLICABLE")
        self.assertEqual(verify_package(self.svc.export_run(run["id"]))["status"], "VERIFIED")


class Jobs(WorkspaceCase):
    def prepare(self):
        s = self.tutorial()
        revs = [self.approve(s["spec"]["id"], k) for k in SPEC_KEYS]
        return s, revs

    def test_cancel_mid_run_discards_partial_output_and_run_can_be_retried(self):
        s, revs = self.prepare()
        gate, reached = threading.Event(), threading.Event()
        def hook(done, total, claim):
            reached.set()
            gate.wait(5)
        self.svc.hooks["after_claim"] = hook
        started = self.svc.start_run(revs, [s["cfg"]["id"]])
        self.assertTrue(reached.wait(10))
        self.assertEqual(self.svc.cancel_job(started["job_id"])["state"], "CANCELLING")
        gate.set()
        job = self.svc.wait_job(started["job_id"])
        self.assertEqual(job["state"], "CANCELLED")
        run = self.svc.get_run(started["run_id"])
        self.assertEqual((run["state"], run["findings"]), ("CANCELLED", []))
        with self.assertRaises(ServiceError) as cm:
            self.svc.export_run(started["run_id"])
        self.assertEqual(cm.exception.code, "run_not_exportable")
        self.svc.hooks.clear()
        retry = self.do_run(revs, [s["cfg"]["id"]])
        self.assertEqual(retry["job"]["state"], "SUCCEEDED")

    def test_cancel_after_success_returns_terminal_state(self):
        s, revs = self.prepare()
        run = self.do_run(revs[:1], [s["cfg"]["id"]])
        self.assertEqual(self.svc.cancel_job(run["job"]["id"])["state"], "SUCCEEDED")

    def test_queued_job_can_be_cancelled(self):
        s, revs = self.prepare()
        svc = self.new_service("one", max_workers=1)
        s2 = self.tutorial(svc)
        r2 = [self.approve(s2["spec"]["id"], k, svc) for k in SPEC_KEYS]
        gate = threading.Event()
        svc.hooks["after_claim"] = lambda *a: gate.wait(5)
        first = svc.start_run(r2, [s2["cfg"]["id"]])
        second = svc.start_run(r2[:1], [s2["cfg"]["id"]])
        self.assertEqual(svc.cancel_job(second["job_id"])["state"], "CANCELLED")
        gate.set()
        self.assertEqual(svc.wait_job(first["job_id"])["state"], "SUCCEEDED")
        self.assertEqual(svc.wait_job(second["job_id"])["state"], "CANCELLED")

    def test_progress_events_are_sequenced_and_resumable(self):
        s, revs = self.prepare()
        run = self.do_run(revs, [s["cfg"]["id"]])
        events = self.svc.job_events(run["job"]["id"])["events"]
        seqs = [e["seq"] for e in events]
        self.assertEqual(seqs, list(range(1, len(seqs) + 1)))
        self.assertEqual(events[-1]["state"], "SUCCEEDED")
        later = self.svc.job_events(run["job"]["id"], after=seqs[2])["events"]
        self.assertEqual([e["seq"] for e in later], seqs[3:])

    def test_backend_restart_marks_abandoned_jobs_failed_without_rerunning(self):
        svc = self.svc
        svc.store.x("INSERT INTO jobs(id,kind,state,stage,payload_json,created_seq,created_at,updated_at) VALUES('job:x','run','RUNNING','evaluating','{}',99,'t','t')")
        svc.close = lambda: None
        from verifier_lab.service import Service
        again = Service(svc.store.root)
        self.addCleanup(again.close)
        job = again.get_job("job:x")
        self.assertEqual((job["state"], job["error"]["code"]), ("FAILED", "abandoned_lease"))

    def test_rule_execution_error_fails_the_job_and_is_not_a_verdict(self):
        from verifier_lab.rules import BaseRule, RuleRegistry, builtin_registry
        class Crash(BaseRule):
            rule_id, predicate = "rule.fact_equals", "fact.equals"
            def evaluate(self, ctx, claim):
                raise RuntimeError("boom")
        reg = RuleRegistry()
        reg.register(Crash())
        svc = self.new_service("crash", registry=reg)
        s = self.tutorial(svc)
        rev = self.approve(s["spec"]["id"], "region", svc)
        run = self.do_run([rev], [s["cfg"]["id"]], svc)
        self.assertEqual((run["job"]["state"], run["state"]), ("FAILED", "FAILED"))
        self.assertEqual(run["findings"], [])
        self.assertEqual(run["errors"][0]["code"], "rule_exception")


if __name__ == "__main__":
    unittest.main()


class IntervalWorkflow(WorkspaceCase):
    SPEC = b"---\nload.start: 0\nload.end: 5\nreport.start: 5\nreport.end: 9\n---\n# Schedule spec (fictional)\n"
    LOG = b'{"load": {"start": 0, "end": 6}, "report": {"start": 5, "end": 9}}'

    def test_reviewed_interval_claim_is_contradicted_with_graph_in_package(self):
        spec, log = self.imp(self.SPEC, "sched.md"), self.imp(self.LOG, "sched-log.json")
        c = self.cand(spec["id"], "load.end")
        rev = self.svc.decide(c["id"], "APPROVE_WITH_EDIT", c["etag"], reason="state the intended ordering",
                              edit={"predicate": "interval.relation", "subject": "load-before-report",
                                    "arguments": {"left": "load", "right": "report", "relation": "precedes", "coord": "s"}}
                              )["claim_revision_id"]
        run = self.do_run([rev], [log["id"]])
        f = run["findings"][0]
        self.assertEqual((f["verdict"], f["reason_code"]), ("CONTRADICTED", "relation_violated"))
        self.assertEqual({e["key"] for e in f["evidence_view"]}, {"load.start", "load.end", "report.start", "report.end"})
        z = self.svc.export_run(run["id"])
        with zipfile.ZipFile(z) as zf:
            result = json.loads(zf.read("result.json"))
        self.assertTrue(result["graph"]["acyclic"] and result["graph"]["closed"])
        check = verify_package(z)
        self.assertEqual(check["status"], "VERIFIED")
        self.assertEqual([u["rule"] for u in check["unsupported"]], ["rule.interval_relation@1"])   # visible, not silently trusted

    def test_bad_interval_edit_rejected_at_review(self):
        spec = self.imp(self.SPEC, "sched.md")
        c = self.cand(spec["id"], "load.end")
        with self.assertRaises(ServiceError):
            self.svc.decide(c["id"], "APPROVE_WITH_EDIT", c["etag"], reason="r",
                            edit={"predicate": "interval.relation", "arguments": {"left": "x"}})

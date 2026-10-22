import json
import shutil
import zipfile
from pathlib import Path

from tests.helpers import TUTORIAL, WorkspaceCase
from verifier_lab import riskpacks as RP
from verifier_lab.canonical import canonical_bytes, sha256_digest
from verifier_lab.checker import verify_package
from verifier_lab.service import ServiceError

PACK = "synthetic-upload-controls"
CONTEXT = {"service.kind": "web", "service.accepts_uploads": True, "service.makes_remote_calls": True,
           "data.stores_personal_email": True, "service.mutates_state": True, "service.tier": "modern"}
# data.is_sensitive is deliberately absent: applicability of DATA-CLASS is UNKNOWN


class PackRun(WorkspaceCase):
    def setUp(self):
        super().setUp()
        s = self.tutorial()
        self.s = s
        spec = s["spec"]["id"]
        self.revs = [
            self.approve(spec, "limits.max_upload_kb"),                                           # CONTRADICTED by config (100 vs 200)
            self.approve(spec, "limits.retries", action="APPROVE_WITH_EDIT", reason="state a bound, not equality",
                         edit={"predicate": "fact.numeric_bound", "arguments": {"op": "lte", "bound": {"kind": "integer", "value": "5"}}}),
            self.approve(spec, "sql.table.users.column.email.not_null"),                          # SUPPORTED by schema.sql
            self.approve(spec, "audit.enabled", action="APPROVE_WITH_EDIT", reason="only presence matters",
                         edit={"predicate": "fact.present", "arguments": {}}),                    # config never states it
        ]

    def go(self, context=CONTEXT, packs=(PACK,), revs=None, svc=None):
        svc = svc or self.svc
        started = svc.start_run(revs or self.revs, [self.s["cfg"]["id"], self.s["sql"]["id"]], pack_ids=list(packs), context=context)
        job = svc.wait_job(started["job_id"])
        self.assertEqual(job["state"], "SUCCEEDED", job["error"])
        return svc.get_run(started["run_id"])

    def ob(self, run):
        return {o["obligation_id"]: o for o in run["obligations"]}

    def test_every_verdict_class_is_reached_with_cited_applicability_facts(self):
        run = self.go()
        o = self.ob(run)
        self.assertEqual({k: v["verdict"] for k, v in o.items()},
                         {"UPLOAD-LIMIT": "CONTRADICTED", "RETRY-BOUND": "SUPPORTED", "REQUIRED-EMAIL": "SUPPORTED",
                          "AUDIT-ENABLED": "INSUFFICIENT_EVIDENCE", "DATA-CLASS": "HUMAN_REVIEW_REQUIRED",
                          "LEGACY-MODE": "NOT_APPLICABLE"})
        self.assertEqual(o["LEGACY-MODE"]["applicability"]["facts"], {"service.kind": "web", "service.tier": "modern"})  # NOT_APPLICABLE cites facts
        self.assertEqual(o["LEGACY-MODE"]["reason"], "applicability_established_false")
        self.assertEqual(o["DATA-CLASS"]["applicability"]["result"], "UNKNOWN")                      # unknown is not false
        self.assertEqual(o["DATA-CLASS"]["reason"], "applicability_requires_human_classification")
        self.assertEqual(o["AUDIT-ENABLED"]["reason"], "member_findings_aggregated")
        self.assertTrue(any("evidence" in r.lower() for r in o["AUDIT-ENABLED"]["remediation"]))
        self.assertEqual(run["risk_packs"][0]["digest"], self.svc.packs.get(PACK).content_digest)
        self.assertEqual(run["context"]["service.kind"], "web")

    def test_package_with_pack_verifies_independently(self):
        run = self.go()
        res = verify_package(self.svc.export_run(run["id"]))
        self.assertEqual(res["status"], "VERIFIED", [c for c in res["checks"] if not c["ok"]])
        names = {c["name"] for c in res["checks"]}
        self.assertIn("obligation:synthetic-upload-controls/UPLOAD-LIMIT", names)
        self.assertIn("context_digest_pinned", names)

    def test_missing_claim_unknown_context_and_pack_level_applicability(self):
        run = self.go(revs=self.revs[:1])                         # only the upload-limit claim approved
        o = self.ob(run)
        self.assertEqual((o["RETRY-BOUND"]["verdict"], o["RETRY-BOUND"]["reason"]), ("INSUFFICIENT_EVIDENCE", "no_claim_for_obligation"))
        self.assertIn("Review and approve a claim", o["RETRY-BOUND"]["remediation"][0])
        run = self.go(context={})                                  # nothing known: nothing is NOT_APPLICABLE
        self.assertEqual({v["verdict"] for v in self.ob(run).values()}, {"INSUFFICIENT_EVIDENCE", "HUMAN_REVIEW_REQUIRED"})
        self.assertTrue(all(v["applicability"]["result"] == "UNKNOWN" for v in self.ob(run).values()))
        self.assertIn("Supply the missing context facts", self.ob(run)["UPLOAD-LIMIT"]["remediation"][0])
        run = self.go(context={**CONTEXT, "service.kind": "desktop"})   # pack applicability FALSE: everything not applicable
        self.assertEqual({v["verdict"] for v in self.ob(run).values()}, {"NOT_APPLICABLE"})

    def test_no_pack_means_no_obligation_layer_and_unchanged_default_flow(self):
        run = self.go(packs=(), context={})
        self.assertEqual(run["obligations"], [])
        self.assertEqual(verify_package(self.svc.export_run(run["id"]))["status"], "VERIFIED")

    def test_bad_pack_selection_and_context_are_rejected_before_any_job(self):
        before = len(self.svc.list_jobs(limit=100))
        for kwargs, code in (({"pack_ids": ["nope"]}, "pack_pack_not_found"), ({"context": {"a": 1.5}}, "pack_bad_context")):
            with self.assertRaises(ServiceError) as cm:
                self.svc.start_run(self.revs, [self.s["cfg"]["id"]], **kwargs)
            self.assertEqual((cm.exception.status, cm.exception.code), (422, code))
        self.assertEqual(len(self.svc.list_jobs(limit=100)), before)                              # nothing was queued

    def test_installing_a_new_version_does_not_change_the_old_report(self):
        run1 = self.go()
        old = self.svc.packs.get(PACK)
        body = json.loads(canonical_bytes(old.body))
        body["version"] = "1.0.1"
        body["obligations"][0]["subject"] = "limits.other"           # a "patch" that changes a requirement
        self.svc.install_pack(canonical_bytes(RP.seal(body)))
        again = self.svc.get_run(run1["id"])
        self.assertEqual(again["freshness"]["state"], "CURRENT")        # the old run still names the exact pinned digest
        self.assertEqual(again["obligations"], run1["obligations"])
        self.assertEqual(verify_package(self.svc.export_run(run1["id"]))["status"], "VERIFIED")
        run2 = self.go()                                                # a re-run picks the newest and is a NEW run
        self.assertNotEqual(run2["id"], run1["id"])
        self.assertEqual(run2["risk_packs"][0]["profile_version"], "1.0.1")
        self.assertEqual(self.svc.list_packs()[-1]["version"], "1.0.1")

    def test_same_inputs_in_two_workspaces_give_identical_pack_bundles(self):
        a = self.go()
        other = self.new_service("ws2")
        s2 = self.tutorial(other)
        spec = s2["spec"]["id"]
        revs = [self.approve(spec, "limits.max_upload_kb", other),
                self.approve(spec, "limits.retries", other, action="APPROVE_WITH_EDIT", reason="state a bound, not equality",
                             edit={"predicate": "fact.numeric_bound", "arguments": {"op": "lte", "bound": {"kind": "integer", "value": "5"}}}),
                self.approve(spec, "sql.table.users.column.email.not_null", other),
                self.approve(spec, "audit.enabled", other, action="APPROVE_WITH_EDIT", reason="only presence matters",
                             edit={"predicate": "fact.present", "arguments": {}})]
        started = other.start_run(revs, [s2["cfg"]["id"], s2["sql"]["id"]], pack_ids=[PACK], context=CONTEXT)
        other.wait_job(started["job_id"])
        self.assertEqual(other.get_run(started["run_id"])["bundle_id"], a["bundle_id"])


class PackTamper(PackRun):
    def setUp(self):
        super().setUp()
        run = self.go()
        z = self.svc.export_run(run["id"])
        self.dir = self.tmp / "pkg"
        with zipfile.ZipFile(z) as zf:
            zf.extractall(self.dir)

    def fresh(self):
        d = self.tmp / "work"
        shutil.rmtree(d, ignore_errors=True)
        shutil.copytree(self.dir, d)
        return d

    def rehash(self, d):
        files = []
        for p in sorted(d.rglob("*")):
            if p.is_file() and p.name not in ("MANIFEST.json", "run_metadata.json"):
                data = p.read_bytes()
                files.append({"path": p.relative_to(d).as_posix(), "sha256": sha256_digest(data), "size": len(data)})
        m = json.loads((d / "MANIFEST.json").read_bytes())
        m["files"] = files
        (d / "MANIFEST.json").write_bytes(canonical_bytes(m))

    def failing(self, d):
        res = verify_package(d)
        self.assertEqual(res["status"], "REJECTED")
        return {c["name"] for c in res["checks"] if not c["ok"]}

    def test_flipped_obligation_verdict_is_rejected(self):
        d = self.fresh()
        r = json.loads((d / "result.json").read_bytes())
        for o in r["obligations"]:
            if o["obligation_id"] == "UPLOAD-LIMIT":
                o["verdict"] = "SUPPORTED"
        (d / "result.json").write_bytes(canonical_bytes(r))
        self.rehash(d)
        self.assertIn("obligation:synthetic-upload-controls/UPLOAD-LIMIT", self.failing(d))

    def test_removed_obligation_and_laundered_not_applicable_are_rejected(self):
        d = self.fresh()
        r = json.loads((d / "result.json").read_bytes())
        r["obligations"] = [o for o in r["obligations"] if o["obligation_id"] != "AUDIT-ENABLED"]
        (d / "result.json").write_bytes(canonical_bytes(r))
        self.rehash(d)
        self.assertIn("obligation_set_complete", self.failing(d))
        d = self.fresh()
        r = json.loads((d / "result.json").read_bytes())
        for o in r["obligations"]:
            if o["obligation_id"] == "AUDIT-ENABLED":
                o["verdict"], o["reason"] = "NOT_APPLICABLE", "applicability_established_false"
        (d / "result.json").write_bytes(canonical_bytes(r))
        self.rehash(d)
        self.assertIn("obligation:synthetic-upload-controls/AUDIT-ENABLED", self.failing(d))

    def test_edited_pack_or_context_artifact_is_rejected_even_with_rehashed_manifest(self):
        d = self.fresh()
        for p in (d / "artifacts").iterdir():
            raw = p.read_bytes()
            if b"synthetic-upload-controls" in raw and b'"obligations"' in raw:
                body = json.loads(raw)
                body["obligations"] = body["obligations"][:-1]           # weaken the pack but keep its pinned identity
                new = canonical_bytes(body)
                p.unlink()
                (d / "artifacts" / sha256_digest(new)[7:]).write_bytes(new)
        self.rehash(d)
        self.failing(d)                                                  # artifact digest no longer matches the bundle record
        d = self.fresh()
        r = json.loads((d / "result.json").read_bytes())
        r["context_digest"] = "sha256:" + "0" * 64
        (d / "result.json").write_bytes(canonical_bytes(r))
        self.rehash(d)
        self.assertIn("context_digest_pinned", self.failing(d))

    def test_obligations_without_a_pinned_pack_are_rejected(self):
        from verifier_lab.canonical import record_id
        d = self.fresh()
        b = json.loads((d / "bundle.json").read_bytes())
        b["profile_refs"] = [p for p in b["profile_refs"] if p["profile_id"] == "verifier-lab.builtin-rules"]
        b["bundle_id"] = record_id("bundle", {k: v for k, v in b.items() if k != "bundle_id"})
        (d / "bundle.json").write_bytes(canonical_bytes(b))
        r = json.loads((d / "result.json").read_bytes())
        r["bundle_id"] = b["bundle_id"]
        (d / "result.json").write_bytes(canonical_bytes(r))
        self.rehash(d)
        self.assertIn("no_risk_packs_no_obligations", self.failing(d))     # the layer cannot exist without its pinned pack

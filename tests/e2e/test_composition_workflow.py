import copy
import json
import shutil
import zipfile
from pathlib import Path

from tests.helpers import ROOT, WorkspaceCase
from verifier_lab.canonical import canonical_bytes, record_id, sha256_digest
from verifier_lab.checker import verify_package
from verifier_lab.service import ServiceError

FIX = ROOT / "fixtures" / "composition"
SPEC = b"---\npipeline.name: demo\n---\n# What the pipeline is meant to do (fictional)\n"


class CompositionRuns(WorkspaceCase):
    def claim(self, svc, spec_id, target=None):
        c = self.cand(spec_id, "pipeline.name", svc)
        args = {"target": target} if target else {}
        return svc.decide(c["id"], "APPROVE_WITH_EDIT", c["etag"], reason="assert the whole pipeline composes",
                          edit={"predicate": "composition.valid", "subject": "pipeline", "arguments": args})["claim_revision_id"]

    def run_fixture(self, name, target=None, svc=None):
        svc = svc or self.svc
        spec = self.imp(SPEC, "spec.md", svc=svc)
        ev = self.imp(FIX / f"{name}.json", svc=svc)
        started = svc.start_run([self.claim(svc, spec["id"], target)], [ev["id"]])
        self.assertEqual(svc.wait_job(started["job_id"])["state"], "SUCCEEDED")
        return svc.get_run(started["run_id"])

    def test_every_fixture_through_review_run_export_and_the_independent_checker(self):
        expect = {"good": "SUPPORTED", "units-mismatch": "CONTRADICTED", "units-converted": "SUPPORTED", "offsets-mismatch": "CONTRADICTED",
                  "missing-input": "INSUFFICIENT_EVIDENCE", "cycle": "CONTRADICTED", "generation-mismatch": "CONTRADICTED"}
        for name, verdict in expect.items():
            svc = self.new_service("w-" + name)
            run = self.run_fixture(name, svc=svc)
            f = run["findings"][0]
            self.assertEqual(f["verdict"], verdict, name)
            res = verify_package(svc.export_run(run["id"]))
            self.assertEqual(res["status"], "VERIFIED", (name, [c for c in res["checks"] if not c["ok"]] + [x for x in res["findings"] if not x["accepted"]]))
            self.assertEqual(res["unsupported"], [])

    def test_targeted_claim_ignores_the_unrelated_broken_branch_but_says_so(self):
        whole = self.run_fixture("scoped")["findings"][0]
        svc = self.new_service("t")
        scoped = self.run_fixture("scoped", "report", svc)["findings"][0]
        self.assertEqual((whole["verdict"], scoped["verdict"]), ("INSUFFICIENT_EVIDENCE", "SUPPORTED"))
        self.assertEqual(scoped["witness"]["analysis"]["scope"]["not_evaluated"], ["side"])
        self.assertEqual(verify_package(svc.export_run(svc.list_runs()[0]["id"]))["status"], "VERIFIED")

    def test_assumptions_travel_with_a_supported_finding(self):
        w = self.run_fixture("good")["findings"][0]["witness"]["analysis"]
        self.assertTrue(w["conditional_on"])

    def test_bad_arguments_and_unreadable_evidence(self):
        spec = self.imp(SPEC, "spec.md")
        c = self.cand(spec["id"], "pipeline.name")
        with self.assertRaises(ServiceError):
            self.svc.decide(c["id"], "APPROVE_WITH_EDIT", c["etag"], reason="r",
                            edit={"predicate": "composition.valid", "subject": "pipeline", "arguments": {"surprise": 1}})
        ev = self.imp(b'{"pipeline": ', "broken.json")
        rev = self.claim(self.svc, spec["id"])
        started = self.svc.start_run([rev], [ev["id"]])
        self.svc.wait_job(started["job_id"])
        self.assertEqual(self.svc.get_run(started["run_id"])["findings"][0]["verdict"], "INVALID_EVIDENCE")

    def test_analysis_and_impact_endpoints_of_the_service(self):
        good = self.imp(FIX / "good.json")
        a = self.svc.composition_analyze(good["id"])
        self.assertEqual(a["analysis"]["overall"], "SUPPORTED")
        self.assertEqual([x["id"] for x in a["activities"]], ["normalize", "index"])
        self.assertEqual([x["depth"] for x in a["activities"]], [0, 1])
        self.assertEqual(self.svc.composition_analyze(good["id"], target="normalize")["analysis"]["scope"]["evaluated"], ["normalize"])
        changed = json.loads((FIX / "good.json").read_text())
        changed["pipeline"]["activities"][0]["produces"][0]["generation"] = "g2"
        new = self.imp(json.dumps(changed).encode(), "good-v2.json")
        res = self.svc.composition_impact(good["id"], new["id"])
        self.assertEqual(res["affected"]["index"], ["normalize", "index"])
        for bad, code in ((("nope", "pipeline"), 404), ((self.imp(SPEC, "x.md")["id"], "pipeline"), 422), ((good["id"], "ghost"), 422)):
            with self.assertRaises(ServiceError) as cm:
                self.svc.composition_analyze(*bad)
            self.assertEqual(cm.exception.status, code)
        with self.assertRaises(ServiceError) as cm:
            self.svc.composition_analyze(good["id"], target="ghost")
        self.assertEqual(cm.exception.code, "bad_target")


class CompositionTamper(CompositionRuns):
    def setUp(self):
        super().setUp()
        run = self.run_fixture("units-mismatch", svc=self.new_service("tam"))
        z = self.tmp / "tam" / "exports"
        svc = self.new_service("tam")
        self.dir = self.tmp / "pkg"
        with zipfile.ZipFile(svc.export_run(run["id"])) as zf:
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

    def test_laundering_a_failed_composition_into_support_is_rejected(self):
        d = self.fresh()
        r = json.loads((d / "result.json").read_bytes())
        for f in r["findings"]:
            f["verdict"], f["reason_code"] = "SUPPORTED", "composition_valid"
            for o in f["witness"]["analysis"]["obligations"]:
                o["verdict"], o["reason"] = "SUPPORTED", "requirements_met" if o["id"].startswith("input") else o["reason"]
            f["witness"]["analysis"]["overall"] = "SUPPORTED"
            f["id"] = record_id("finding", {k: v for k, v in f.items() if k != "id"})        # self-consistent forgery
        (d / "result.json").write_bytes(canonical_bytes(r))
        self.rehash(d)
        res = verify_package(d)
        self.assertEqual(res["status"], "REJECTED")
        self.assertTrue(any("re-derived CONTRADICTED" in f["detail"] for f in res["findings"]))

    def test_hiding_an_obligation_is_rejected_even_if_the_verdict_stays_right(self):
        d = self.fresh()
        r = json.loads((d / "result.json").read_bytes())
        for f in r["findings"]:
            f["witness"]["analysis"]["obligations"] = f["witness"]["analysis"]["obligations"][:-1] + []
            f["witness"]["analysis"]["obligations"].insert(0, f["witness"]["analysis"]["obligations"][0])
            f["id"] = record_id("finding", {k: v for k, v in f.items() if k != "id"})
        (d / "result.json").write_bytes(canonical_bytes(r))
        self.rehash(d)
        self.assertEqual(verify_package(d)["status"], "REJECTED")

import copy
import json
import shutil
import zipfile

from tests.helpers import ROOT, WorkspaceCase
from verifier_lab import purposes as PU
from verifier_lab.canonical import canonical_bytes, sha256_digest
from verifier_lab.checker import verify_package
from verifier_lab.service import ServiceError

FIX = ROOT / "fixtures" / "purposes"
BIND = {"entries_count": ["checkpoint.entries_count"], "allocator_next": ["checkpoint.allocator_next"],
        "random_cursor": ["checkpoint.random_cursor"], "operation_cursor": ["checkpoint.operation_cursor"],
        "semantics_version": ["checkpoint.semantics_version"]}
WEAK_BIND = {"entries_count": BIND["entries_count"]}
REQS = [{"profile_id": "inspect-checkpoint", "bindings": WEAK_BIND}, {"profile_id": "resume-exactly", "bindings": BIND}]


class PurposeRuns(WorkspaceCase):
    def run_with(self, names, reqs=REQS, svc=None):
        svc = svc or self.svc
        ids = [self.imp(FIX / n, svc=svc)["id"] for n in names]
        started = svc.start_run([], ids, purposes=reqs)
        job = svc.wait_job(started["job_id"])
        self.assertEqual(job["state"], "SUCCEEDED", job["error"])
        return svc.get_run(started["run_id"])

    def by_profile(self, run):
        return {p["profile"]["profile_id"]: p for p in run["purposes"]}

    def test_one_package_two_purposes_through_the_whole_pipeline_and_the_checker(self):
        run = self.run_with(["map.json", "counters.json", "history.json"])
        p = self.by_profile(run)
        self.assertEqual((p["inspect-checkpoint"]["overall"], p["resume-exactly"]["overall"]), ("SUPPORTED", "SUPPORTED"))
        res = verify_package(self.svc.export_run(run["id"]))
        self.assertEqual(res["status"], "VERIFIED", [c for c in res["checks"] if not c["ok"]])
        self.assertIn("purpose:resume-exactly", {c["name"] for c in res["checks"]})

    def test_incomplete_torn_invalid_and_invalidated_packages_each_get_the_justified_outcome(self):
        cases = {("map.json",): "INSUFFICIENT_EVIDENCE", ("map.json", "counters.json", "history-invalidated.json"): "CONTRADICTED",
                 ("map.json", "counters-g6.json", "history.json"): "CONTRADICTED",
                 ("map.json", "counters-bad-type.json", "history.json"): "INVALID_EVIDENCE",
                 ("map.json", "counters.json", "history-other-clock.json"): "INSUFFICIENT_EVIDENCE"}
        for names, expected in cases.items():
            svc = self.new_service("w" + str(abs(hash(names))))
            run = self.run_with(list(names), svc=svc)
            p = self.by_profile(run)
            self.assertEqual(p["inspect-checkpoint"]["overall"], "SUPPORTED", names)       # the weak purpose is met by the same package
            self.assertEqual(p["resume-exactly"]["overall"], expected, names)
            self.assertEqual(verify_package(svc.export_run(run["id"]))["status"], "VERIFIED", names)   # checker re-derives each one

    def test_purpose_only_run_needs_no_claims_but_still_requires_evidence(self):
        run = self.run_with(["map.json"], reqs=REQS[:1])
        self.assertEqual(run["findings"], [])
        with self.assertRaises(ServiceError) as cm:
            self.svc.start_run([], [], purposes=REQS[:1])
        self.assertEqual(cm.exception.code, "empty_selection")
        with self.assertRaises(ServiceError) as cm:
            self.svc.start_run([], [self.imp(FIX / "map.json")["id"]])
        self.assertEqual(cm.exception.code, "empty_selection")

    def test_bad_purpose_requests_are_rejected_before_any_job(self):
        sid = self.imp(FIX / "map.json")["id"]
        before = len(self.svc.list_jobs(limit=100))
        for req, code in (([{"profile_id": "nope", "bindings": {}}], "purpose_profile_not_found"),
                          ([{"profile_id": "inspect-checkpoint", "bindings": {"ghost": ["x"]}}], "purpose_unknown_role"),
                          ([{"profile_id": "inspect-checkpoint", "bindings": {"entries_count": []}}], "purpose_bad_bindings"),
                          ([{"bindings": {}}], "purpose_bad_request")):
            with self.assertRaises(ServiceError) as cm:
                self.svc.start_run([], [sid], purposes=req)
            self.assertEqual((cm.exception.status, cm.exception.code), (422, code))
        self.assertEqual(len(self.svc.list_jobs(limit=100)), before)

    def test_older_reports_keep_their_profile_and_a_newer_version_is_offered_not_applied(self):
        run = self.run_with(["map.json", "counters.json", "history.json"])
        newer = copy.deepcopy(PU.PurposeCatalog(None).get("resume-exactly").body)
        newer["version"] = "1.1.0"
        newer["roles"].append({"role": "extra", "min": 1, "max": 1, "value_kind": "string"})
        self.svc.install_purpose(canonical_bytes(PU.seal(newer)))
        again = self.svc.get_run(run["id"])
        p = self.by_profile(again)["resume-exactly"]
        self.assertEqual((p["profile"]["profile_version"], p["newer_version"], p["overall"]), ("1.0.0", "1.1.0", "SUPPORTED"))
        self.assertEqual(again["freshness"]["state"], "CURRENT")                              # never silently re-evaluated
        self.assertEqual(verify_package(self.svc.export_run(run["id"]))["status"], "VERIFIED")

    def test_same_inputs_in_two_workspaces_give_identical_bundles(self):
        a = self.run_with(["map.json", "counters.json", "history.json"])
        b = self.run_with(["map.json", "counters.json", "history.json"], svc=self.new_service("ws2"))
        self.assertEqual(a["bundle_id"], b["bundle_id"])


class PurposeTamper(PurposeRuns):
    def setUp(self):
        super().setUp()
        run = self.run_with(["map.json", "counters-g6.json", "history.json"])    # resume-exactly is CONTRADICTED here
        self.dir = self.tmp / "pkg"
        with zipfile.ZipFile(self.svc.export_run(run["id"])) as zf:
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

    def edit_result(self, fn):
        d = self.fresh()
        r = json.loads((d / "result.json").read_bytes())
        fn(r)
        (d / "result.json").write_bytes(canonical_bytes(r))
        self.rehash(d)
        return d

    def test_laundering_a_torn_generation_into_support_is_rejected(self):
        def launder(r):
            for p in r["purposes"]:
                if p["profile"]["profile_id"] == "resume-exactly":
                    p["overall"] = "SUPPORTED"
                    for o in p["obligations"]:
                        o["verdict"], o["reason"] = "SUPPORTED", "sources_coherent" if o["kind"] == "coherence" else o["reason"]
        self.assertIn("purpose:resume-exactly", self.failing(self.edit_result(launder)))

    def test_dropped_or_reordered_obligations_and_hidden_assessments_are_rejected(self):
        def drop(r):
            for p in r["purposes"]:
                p["obligations"] = [o for o in p["obligations"] if o["kind"] != "coherence"]
        self.assertIn("purpose:resume-exactly", self.failing(self.edit_result(drop)))

        def hide(r):
            r["purposes"] = r["purposes"][:1]
        self.assertIn("purpose_set_complete", self.failing(self.edit_result(hide)))

        def invent(r):
            r["purposes"] = r["purposes"] + [{**r["purposes"][0], "profile": {**r["purposes"][0]["profile"], "profile_id": "ghost"}}]
        self.assertIn("purpose_set_complete", self.failing(self.edit_result(invent)))

    def test_assessments_without_a_pinned_purpose_and_edited_profile_are_rejected(self):
        d = self.fresh()
        for p in (d / "artifacts").iterdir():
            raw = p.read_bytes()
            if b'"lifecycle"' in raw and b'"roles"' in raw:
                body = json.loads(raw)
                body["coherence"] = []                                          # weaken the purpose but keep its pinned identity
                new = canonical_bytes(body)
                p.unlink()
                (d / "artifacts" / sha256_digest(new)[7:]).write_bytes(new)
        self.rehash(d)
        self.failing(d)

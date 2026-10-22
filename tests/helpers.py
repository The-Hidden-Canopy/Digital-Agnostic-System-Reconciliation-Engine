"""Shared test helpers. Fixtures are fictional; see fixtures/README.md for intended verdicts."""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from verifier_lab.service import Service

ROOT = Path(__file__).resolve().parents[1]
TUTORIAL = ROOT / "fixtures" / "tutorial"


class WorkspaceCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vl-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.svc = self.new_service()

    def new_service(self, name: str = "ws", **kw) -> Service:
        svc = Service(self.tmp / name, **kw)
        self.addCleanup(svc.close)
        return svc

    def imp(self, data: bytes | Path, name: str | None = None, svc: Service | None = None, **kw) -> dict:
        svc = svc or self.svc
        if isinstance(data, Path):
            name, data = name or data.name, data.read_bytes()
        res = svc.import_source(data, name, **kw)
        if res["job_id"]:
            job = svc.wait_job(res["job_id"])
            self.assertEqual(job["state"], "SUCCEEDED", job.get("error"))
        return svc.get_source(res["source"]["id"], detail=True)

    def cand(self, source_id: str, key: str, svc: Service | None = None) -> dict:
        svc = svc or self.svc
        items = [c for c in svc.list_candidates(source_id=source_id, limit=500)["items"] if c["fact_key"] == key]
        self.assertEqual(len(items), 1, f"{key}: {len(items)} candidates")
        return items[0]

    def approve(self, source_id: str, key: str, svc: Service | None = None, **kw) -> str:
        svc = svc or self.svc
        c = self.cand(source_id, key, svc)
        out = svc.decide(c["id"], kw.pop("action", "APPROVE"), c["etag"], **kw)
        return out["claim_revision_id"]

    def do_run(self, revs: list[str], evidence: list[str], svc: Service | None = None) -> dict:
        svc = svc or self.svc
        started = svc.start_run(revs, evidence)
        job = svc.wait_job(started["job_id"])
        return {"job": job, **svc.get_run(started["run_id"]), "started": started}

    def tutorial(self, svc: Service | None = None) -> dict:
        svc = svc or self.svc
        return {"spec": self.imp(TUTORIAL / "product-spec.md", svc=svc),
                "cfg": self.imp(TUTORIAL / "service-config.json", svc=svc),
                "sql": self.imp(TUTORIAL / "schema.sql", svc=svc, dialect="postgresql"),
                "pdf": self.imp(TUTORIAL / "ops-note.pdf", svc=svc)}


def verdict_of(run: dict, subject: str) -> str:
    hits = [f for f in run["findings"] if f["claim"]["subject"] == subject]
    assert len(hits) == 1, (subject, len(hits))
    return hits[0]["verdict"]

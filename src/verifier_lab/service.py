"""Application service shared by the HTTP API, the CLI and tests (one interpretation of results)."""
from __future__ import annotations

import base64
import hashlib
import json
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import __version__
from .canonical import canonical_bytes, record_id, sha256_digest
from .engine import CheckPlan, check_claims
from .graph import inspect_bundle
from .export import (LIMITATIONS, render_report, runtime_identity, write_package, zip_package)
from .ingestion import values as V
from .ingestion.admit import AdmissionError, AdmissionLimits, admit, read_blob, store_blob
from .ingestion.common import Limits
from .ingestion.extract import ExtractionFailure, build_record, extract_isolated
from .model import (Activity, Artifact, Assumption, Bundle, Claim, ProfileRef, Verdict)
from . import purposes as PU
from . import riskpacks as RP
from .rules import (PURPOSE_BINDINGS_MEDIA, PURPOSE_MEDIA, CONTEXT_MEDIA, EXTRACT_OP, EXTRACTION_MEDIA, PROFILE_ID, PROFILE_VERSION, PROVENANCE_MEDIA,
                    RISKPACK_MEDIA, CheckContext, builtin_registry)
from .store import Store, jdump, jload, now_iso

MEDIA = {"pdf": "application/pdf", "json": "application/json", "markdown": "text/markdown",
         "sql": "application/sql"}
APPROVE = ("APPROVE", "APPROVE_WITH_EDIT")
ACTIONS = APPROVE + ("REJECT", "DEFER")
TERMINAL = ("SUCCEEDED", "FAILED", "CANCELLED")


class ServiceError(Exception):
    def __init__(self, status: int, code: str, message: str = "", **details):
        super().__init__(f"{code}: {message}")
        self.status, self.code, self.message, self.details = status, code, message, details

    def to_dict(self) -> dict:
        return {"error": {"code": self.code, "message": self.message, **self.details}}


class JobCancelled(Exception):
    pass


class JobContext:
    def __init__(self, svc: "Service", job_id: str):
        self.svc, self.job_id = svc, job_id

    def progress(self, stage: str, done: int | None = None, total: int | None = None):
        self.svc._job_update(self.job_id, stage=stage, done=done, total=total, event="progress")

    def cancelled(self) -> bool:
        row = self.svc.store.q1("SELECT cancel_requested FROM jobs WHERE id=?", (self.job_id,))
        return bool(row and row[0])


class Service:
    def __init__(self, workspace: Path, *, limits: Limits | None = None, registry=None,
                 max_workers: int = 2, admission: AdmissionLimits | None = None):
        self.store = Store(Path(workspace))
        self.limits = limits or Limits()
        self.admission = admission or AdmissionLimits()
        self.registry = registry or builtin_registry()
        self.purposes = PU.PurposeCatalog(self.store.root / "purposes")
        self.packs = RP.PackCatalog(self.store.root / "packs", self.registry.implemented_predicates())
        self.executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="vl-job")
        self.hooks: dict = {}
        self._recover()

    def close(self):
        self.executor.shutdown(wait=True, cancel_futures=False)
        self.store.close()

    # ------------------------------------------------------------------ jobs
    def _recover(self):
        """Backend restart: abandoned leases are marked explicitly, never silently re-run."""
        for row in self.store.q("SELECT id, kind, payload_json FROM jobs WHERE state IN ('QUEUED','RUNNING','CANCELLING')"):
            self._job_update(row["id"], state="FAILED", error={"code": "abandoned_lease",
                             "message": "the backend restarted while this job was active", "retryable": True},
                             event="state")
            payload = jload(row["payload_json"]) or {}
            if row["kind"] == "import" and payload.get("attempt_id"):
                self.store.x("UPDATE attempts SET state='FAILED', error_json=? WHERE id=? AND state IN ('QUEUED','PARSING')",
                             (jdump({"code": "abandoned_lease"}), payload["attempt_id"]))
            if row["kind"] == "run" and payload.get("run_id"):
                self.store.x("UPDATE runs SET state='FAILED' WHERE id=? AND state IN ('QUEUED','RUNNING')",
                             (payload["run_id"],))

    def _job_update(self, job_id: str, *, state=None, stage=None, done=None, total=None, error=None,
                    result=None, event="state"):
        with self.store.tx() as db:
            row = db.execute("SELECT state FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                return
            sets, args = ["updated_at=?", "heartbeat=?"], [now_iso(), now_iso()]
            for col, val in (("state", state), ("stage", stage), ("done", done), ("total", total)):
                if val is not None:
                    sets.append(f"{col}=?")
                    args.append(val)
            if error is not None:
                sets.append("error_json=?")
                args.append(jdump(error))
            if result is not None:
                sets.append("result_json=?")
                args.append(jdump(result))
            db.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE id=?", (*args, job_id))
            seq = (db.execute("SELECT COALESCE(MAX(seq),0)+1 FROM job_events WHERE job_id=?", (job_id,)).fetchone()[0])
            snap = db.execute("SELECT state, stage, done, total FROM jobs WHERE id=?", (job_id,)).fetchone()
            db.execute("INSERT INTO job_events VALUES(?,?,?,?)",
                       (job_id, seq, event, jdump({"state": snap[0], "stage": snap[1], "done": snap[2],
                                                   "total": snap[3], "error": error})))

    def _submit(self, kind: str, payload: dict, fn) -> str:
        seq = self.store.next_seq("job")
        job_id = record_id("job", {"kind": kind, "seq": seq})
        ts = now_iso()
        self.store.x("INSERT INTO jobs(id,kind,state,stage,payload_json,created_seq,created_at,updated_at,heartbeat)"
                     " VALUES(?,?,?,?,?,?,?,?,?)", (job_id, kind, "QUEUED", "queued", jdump(payload), seq, ts, ts, ts))
        self._job_update(job_id, event="state")
        self.executor.submit(self._run_job, job_id, fn)
        return job_id

    def _run_job(self, job_id: str, fn):
        with self.store.tx() as db:
            row = db.execute("SELECT state, cancel_requested FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row["state"] != "QUEUED":
                return
            db.execute("UPDATE jobs SET state='RUNNING', stage='starting' WHERE id=?", (job_id,))
        self._job_update(job_id, state="RUNNING", stage="starting")
        ctx = JobContext(self, job_id)
        try:
            result = fn(ctx)
            self._job_update(job_id, state="SUCCEEDED", stage="done", result=result or {})
        except JobCancelled:
            self._job_update(job_id, state="CANCELLED", stage="cancelled")
        except ExtractionFailure as exc:
            self._job_update(job_id, state="FAILED", stage="failed",
                             error={"code": exc.code, "message": str(exc), "retryable": exc.code in ("wall_clock_timeout",)})
        except Exception as exc:
            self._job_update(job_id, state="FAILED", stage="failed",
                             error={"code": "job_exception", "message": f"{type(exc).__name__}: {exc}"[:500],
                                    "retryable": False})

    def get_job(self, job_id: str) -> dict:
        row = self.store.q1("SELECT * FROM jobs WHERE id=?", (job_id,))
        if not row:
            raise ServiceError(404, "job_not_found")
        return self._job_view(row)

    def _job_view(self, row) -> dict:
        return {"id": row["id"], "kind": row["kind"], "state": row["state"], "stage": row["stage"],
                "done": row["done"], "total": row["total"], "error": jload(row["error_json"]),
                "result": jload(row["result_json"]), "heartbeat": row["heartbeat"],
                "created_at": row["created_at"], "payload": jload(row["payload_json"])}

    def list_jobs(self, limit: int = 50) -> list[dict]:
        return [self._job_view(r) for r in self.store.q("SELECT * FROM jobs ORDER BY created_seq DESC LIMIT ?", (limit,))]

    def job_events(self, job_id: str, after: int = 0) -> dict:
        self.get_job(job_id)
        rows = self.store.q("SELECT seq,type,body_json FROM job_events WHERE job_id=? AND seq>? ORDER BY seq", (job_id, after))
        return {"events": [{"seq": r["seq"], "type": r["type"], **jload(r["body_json"])} for r in rows]}

    def cancel_job(self, job_id: str) -> dict:
        with self.store.tx() as db:
            row = db.execute("SELECT state FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                raise ServiceError(404, "job_not_found")
            if row["state"] == "QUEUED":
                db.execute("UPDATE jobs SET state='CANCELLED', stage='cancelled', cancel_requested=1 WHERE id=?", (job_id,))
            elif row["state"] == "RUNNING":
                db.execute("UPDATE jobs SET state='CANCELLING', stage='cancelling', cancel_requested=1 WHERE id=?", (job_id,))
        self._job_update(job_id, event="state")
        return self.get_job(job_id)

    def wait_job(self, job_id: str, timeout: float = 120.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = self.get_job(job_id)
            if job["state"] in TERMINAL:
                return job
            time.sleep(0.02)
        raise TimeoutError(job_id)

    # --------------------------------------------------------------- idempotency
    def _idem(self, scope: str, key: str | None, payload) -> dict | None:
        if not key:
            return None
        h = sha256_digest(canonical_bytes(payload))
        row = self.store.q1("SELECT payload_hash, response_json FROM idempotency WHERE scope=? AND key=?", (scope, key))
        if row is None:
            return None
        if row["payload_hash"] != h:
            raise ServiceError(422, "idempotency_key_reuse", "this key was used with a different payload")
        return jload(row["response_json"])

    def _idem_store(self, scope: str, key: str | None, payload, response: dict):
        if key:
            self.store.x("INSERT OR IGNORE INTO idempotency VALUES(?,?,?,?)",
                         (scope, key, sha256_digest(canonical_bytes(payload)), jdump(response)))

    # ------------------------------------------------------------------- import
    def import_source(self, data: bytes, filename: str | None, declared_type: str | None = None,
                      dialect: str | None = None, replaces: str | None = None) -> dict:
        try:
            adm = admit(data, filename, declared_type, self.store.blobs, self.admission)
        except AdmissionError as exc:
            status = 413 if exc.code == "size_limit" else 415 if exc.code in (
                "archive_not_supported", "unsupported_format") else 422
            raise ServiceError(status, exc.code, exc.message)
        source_id = "source:" + adm.digest
        if adm.detected_format == "sql":
            dialect = dialect or "unresolved"
        else:
            dialect = None
        if dialect and dialect not in ("generic", "postgresql", "mysql", "sqlite", "unresolved"):
            raise ServiceError(422, "unsupported_dialect", dialect)
        if replaces and not self.store.q1("SELECT 1 FROM sources WHERE id=?", (replaces,)):
            raise ServiceError(404, "replaced_source_not_found")
        with self.store.tx() as db:
            row = db.execute("SELECT id FROM sources WHERE id=?", (source_id,)).fetchone()
            if row is None:
                seq = self.store.next_seq("source")
                db.execute("INSERT INTO sources(id,digest,size,display_name,detected_format,created_seq,created_at,replaces)"
                           " VALUES(?,?,?,?,?,?,?,?)", (source_id, adm.digest, adm.size, adm.display_name,
                                                        adm.detected_format, seq, now_iso(), replaces))
            existing = db.execute(
                "SELECT id, state FROM attempts WHERE source_id=? AND COALESCE(dialect,'')=? AND state IN ('QUEUED','PARSING','EXTRACTED') ORDER BY created_seq DESC LIMIT 1",
                (source_id, dialect or "")).fetchone()
            if existing:
                attempt_id, job = existing["id"], None
                deduplicated = True
            else:
                deduplicated = False
                aseq = self.store.next_seq("attempt")
                attempt_id = record_id("attempt", {"source": adm.digest, "dialect": dialect, "n": aseq})
                db.execute("INSERT INTO attempts(id,source_id,dialect,state,created_seq,created_at) VALUES(?,?,?,?,?,?)",
                           (attempt_id, source_id, dialect, "QUEUED", aseq, now_iso()))
            if replaces and not deduplicated:
                pass
        job_id = None
        if not deduplicated:
            payload = {"source_id": source_id, "attempt_id": attempt_id, "replaces": replaces}
            job_id = self._submit("import", payload,
                                  lambda ctx: self._extract_job(ctx, source_id, attempt_id, adm.detected_format,
                                                                dialect, replaces))
        return {"source": self.get_source(source_id), "attempt_id": attempt_id, "job_id": job_id,
                "deduplicated": deduplicated}

    def reextract(self, source_id: str, dialect: str | None = None) -> dict:
        src = self._source_row(source_id)
        data = read_blob(self.store.blobs, src["digest"])
        return self.import_source(data, src["display_name"], src["detected_format"], dialect)

    def _extract_job(self, ctx: JobContext, source_id: str, attempt_id: str, fmt: str, dialect: str | None,
                     replaces: str | None) -> dict:
        src = self._source_row(source_id)
        self.store.x("UPDATE attempts SET state='PARSING' WHERE id=?", (attempt_id,))
        ctx.progress("parsing")
        data = read_blob(self.store.blobs, src["digest"])
        try:
            wire = extract_isolated(fmt, data, dialect, self.limits, ctx.cancelled)
        except ExtractionFailure as exc:
            state = "CANCELLED" if exc.code == "cancelled" else "FAILED"
            self.store.x("UPDATE attempts SET state=?, error_json=? WHERE id=?",
                         (state, jdump({"code": exc.code, "message": str(exc)}), attempt_id))
            if exc.code == "cancelled":
                raise JobCancelled()
            raise
        ctx.progress("recording")
        record, rendition = build_record(src["digest"], data, wire, self.limits, {"dialect": dialect})
        record_bytes = canonical_bytes(record)
        store_blob(self.store.blobs, sha256_digest(record_bytes), record_bytes)
        store_blob(self.store.blobs, record["rendition_digest"], rendition)
        manifest_id = record["manifest"]["id"]
        with self.store.tx() as db:  # one atomic publish of the whole attempt
            db.execute("UPDATE attempts SET state='EXTRACTED', manifest_id=?, summary=?, record_digest=?, rendition_digest=? WHERE id=?",
                       (manifest_id, record["coverage"]["summary"], sha256_digest(record_bytes),
                        record["rendition_digest"], attempt_id))
            for i, fact in enumerate(record["facts"]):
                pred = {"predicate": "fact.equals", "subject": fact["key"], "arguments": {"expected": fact["value"]}}
                cid = record_id("candidate", {"manifest_id": manifest_id, "span_id": fact["span_id"]})
                db.execute("INSERT OR IGNORE INTO candidates VALUES(?,?,?,?,?,?,?,?)",
                           (cid, attempt_id, source_id, fact["key"], jdump(pred), fact["span_id"],
                            jdump(fact.get("qualifiers", [])), i))
            db.execute("UPDATE sources SET current_attempt=? WHERE id=?", (attempt_id, source_id))
            if replaces:
                db.execute("UPDATE sources SET superseded_by=? WHERE id=? AND id<>?", (source_id, replaces, source_id))
        return {"source_id": source_id, "attempt_id": attempt_id, "coverage": record["coverage"]["summary"],
                "facts": len(record["facts"])}

    # ------------------------------------------------------------------ sources
    def _source_row(self, source_id: str):
        row = self.store.q1("SELECT * FROM sources WHERE id=?", (source_id,))
        if not row:
            raise ServiceError(404, "source_not_found")
        return row

    def _record_for_attempt(self, attempt) -> dict | None:
        if not attempt or not attempt["record_digest"]:
            return None
        return json.loads(read_blob(self.store.blobs, attempt["record_digest"]))

    def get_source(self, source_id: str, detail: bool = False) -> dict:
        row = self._source_row(source_id)
        attempts = self.store.q("SELECT * FROM attempts WHERE source_id=? ORDER BY created_seq", (source_id,))
        current = next((a for a in attempts if a["id"] == row["current_attempt"]), None) or (attempts[-1] if attempts else None)
        counts = Counter()
        if current:
            for r in self.store.q("SELECT id FROM candidates WHERE attempt_id=?", (current["id"],)):
                counts[self._candidate_status(r["id"])] += 1
        view = {"id": row["id"], "digest": row["digest"], "size": row["size"], "name": row["display_name"],
                "format": row["detected_format"], "created_at": row["created_at"],
                "superseded_by": row["superseded_by"], "replaces": row["replaces"],
                "attempt": None if not current else {
                    "id": current["id"], "state": current["state"], "coverage": current["summary"],
                    "dialect": current["dialect"], "error": jload(current["error_json"])},
                "attempts": len(attempts), "candidate_counts": dict(counts)}
        if detail and current:
            rec = self._record_for_attempt(current)
            if rec:
                view["manifest"] = rec["manifest"]
                view["coverage_regions"] = rec["coverage"]["regions"]
                view["diagnostics"] = rec["diagnostics"]
                view["blocked_keys"] = rec["blocked_keys"]
                view["fact_count"] = len(rec["facts"])
        return view

    def list_sources(self) -> list[dict]:
        return [self.get_source(r["id"]) for r in self.store.q("SELECT id FROM sources ORDER BY created_seq DESC")]

    def source_text(self, source_id: str, max_chars: int = 300_000) -> dict:
        row = self._source_row(source_id)
        attempt = self.store.q1("SELECT * FROM attempts WHERE id=?", (row["current_attempt"],)) if row["current_attempt"] else None
        digest = attempt["rendition_digest"] if attempt and attempt["rendition_digest"] else row["digest"]
        if row["detected_format"] == "pdf" and not (attempt and attempt["rendition_digest"]):
            return {"text": "", "rendition_digest": None, "truncated": False, "note": "no text rendition available"}
        data = read_blob(self.store.blobs, digest)
        text = data[:max_chars * 4].decode("utf-8", errors="replace")
        return {"text": text[:max_chars], "rendition_digest": digest, "truncated": len(text) > max_chars,
                "note": "inert text; never rendered as markup"}

    # --------------------------------------------------------------- candidates
    def author_candidate(self, source_id: str, attempt_id: str, byte_start: int, byte_end: int,
                         interpretation: dict, reason: str, *, idempotency_key: str | None = None) -> dict:
        """Propose a human interpretation; never modify extraction facts or approve automatically."""
        if not isinstance(source_id, str) or not isinstance(attempt_id, str):
            raise ServiceError(422, "bad_author_request", "source and attempt identifiers must be strings")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 4000:
            raise ServiceError(422, "author_reason_required", "explain the interpretation in 1–4000 characters")
        if not isinstance(interpretation, dict) or set(interpretation) != {"predicate", "subject", "arguments"}:
            raise ServiceError(422, "bad_interpretation", "predicate, subject and arguments are required")
        final = self._validate_edit({}, interpretation)
        payload = {"source_id": source_id, "attempt_id": attempt_id, "byte_start": byte_start,
                   "byte_end": byte_end, "interpretation": final, "reason": reason.strip()}
        with self.store.tx() as db:
            prior = self._idem("author_candidate", idempotency_key, payload)
            if prior is not None:
                return self.get_candidate(prior["id"])
            src = self._source_row(source_id)
            if src["superseded_by"] or src["current_attempt"] != attempt_id:
                raise ServiceError(409, "source_changed", "reload the source before selecting a span")
            att = db.execute("SELECT * FROM attempts WHERE id=? AND source_id=?", (attempt_id, source_id)).fetchone()
            if not att or att["state"] != "EXTRACTED":
                raise ServiceError(409, "source_not_ready")
            rec = self._record_for_attempt(att)
            rend = read_blob(self.store.blobs, rec["rendition_digest"])
            if (type(byte_start) is not int or type(byte_end) is not int or
                    not 0 <= byte_start < byte_end <= len(rend) or byte_end - byte_start > 16000):
                raise ServiceError(422, "bad_source_span", "select 1–16000 bytes within the current rendition")
            try:
                rend[:byte_start].decode("utf-8")
                text = rend[byte_start:byte_end].decode("utf-8")
                if not text.strip():
                    raise ValueError("blank source selection")
            except (UnicodeError, ValueError) as exc:
                raise ServiceError(422, "bad_source_span", "selection must contain text on UTF-8 boundaries") from exc
            loc = {"byte_start": byte_start, "byte_end": byte_end}
            span_id = record_id("span", {"source_digest": rec["source_digest"],
                "rendition_digest": rec["rendition_digest"], "extraction_manifest_id": rec["manifest"]["id"],
                "locator": loc})
            cid = record_id("authored-candidate", payload)
            fact = {"key": final["subject"], "value": {"kind": "string", "value": text},
                    "grammar": "reviewed-text", "span_id": span_id, "locator": loc,
                    "extraction_method": "human-selected-text", "token_digest": sha256_digest(rend[byte_start:byte_end])}
            db.execute("INSERT OR IGNORE INTO authored_spans VALUES(?,?,?,?,?)",
                       (cid, attempt_id, span_id, jdump(fact), reason.strip()))
            ordinal = db.execute("SELECT COALESCE(MAX(ord),0)+1 FROM candidates WHERE attempt_id=?", (attempt_id,)).fetchone()[0]
            db.execute("INSERT OR IGNORE INTO candidates VALUES(?,?,?,?,?,?,?,?)", (cid, attempt_id, source_id,
                       final["subject"], jdump(final), span_id,
                       jdump(["human interpretation of selected text; semantic entailment is not machine-verified"]), ordinal))
            self._idem_store("author_candidate", idempotency_key, payload, {"id": cid})
        return self.get_candidate(cid)

    def _origin_fact(self, rec: dict, attempt_id: str, span_id: str, candidate_id: str | None = None):
        if candidate_id:
            authored = self.store.q1("SELECT fact_json FROM authored_spans WHERE candidate_id=? AND attempt_id=? AND span_id=?",
                                       (candidate_id, attempt_id, span_id))
            if authored:
                return jload(authored[0])
        return next((f for f in rec["facts"] if f["span_id"] == span_id), None)

    def _decisions(self, candidate_id: str):
        return self.store.q("SELECT * FROM decisions WHERE candidate_id=? ORDER BY n", (candidate_id,))

    def _candidate_status(self, candidate_id: str) -> str:
        last = self.store.q1("SELECT action FROM decisions WHERE candidate_id=? ORDER BY n DESC LIMIT 1", (candidate_id,))
        if not last:
            return "PENDING"
        return {"APPROVE": "APPROVED", "APPROVE_WITH_EDIT": "APPROVED", "REJECT": "REJECTED", "DEFER": "DEFERRED"}[last[0]]

    def _span_view(self, attempt_id: str, span_id: str, context: int = 300, candidate_id: str | None = None) -> dict | None:
        att = self.store.q1("SELECT * FROM attempts WHERE id=?", (attempt_id,))
        rec = self._record_for_attempt(att)
        if not rec:
            return None
        fact = self._origin_fact(rec, attempt_id, span_id, candidate_id)
        if not fact:
            return None
        rend = read_blob(self.store.blobs, rec["rendition_digest"])
        loc = fact["locator"]
        a, b = loc["byte_start"], loc["byte_end"]
        lo, hi = max(0, a - context), min(len(rend), b + context)
        dec = lambda x: x.decode("utf-8", errors="replace")
        return {"span_id": span_id, "artifact_hash": rec["source_digest"], "rendition_hash": rec["rendition_digest"],
                "locator": loc, "text": dec(rend[a:b]), "before": dec(rend[lo:a]), "after": dec(rend[b:hi]),
                "extraction_method": fact["extraction_method"], "extraction_confidence": None,
                "key": fact["key"], "value": fact["value"]}

    def _candidate_view(self, row, detail: bool = False) -> dict:
        decisions = self._decisions(row["id"])
        src = self._source_row(row["source_id"])
        pred = jload(row["predicate_json"])
        view = {"id": row["id"], "source_id": row["source_id"], "source_name": src["display_name"],
                "source_format": src["detected_format"], "attempt_id": row["attempt_id"],
                "fact_key": row["fact_key"], "predicate": pred, "qualifiers": jload(row["qualifiers_json"]),
                "status": self._candidate_status(row["id"]), "etag": f'"c{len(decisions)}"',
                "extraction_confidence": None,
                "current": src["current_attempt"] == row["attempt_id"],
                "claim_revision_id": None}
        rev = self.store.q1("SELECT id FROM claim_revisions WHERE candidate_id=? AND status='CURRENT'", (row["id"],))
        view["claim_revision_id"] = rev["id"] if rev else None
        authored = self.store.q1("SELECT reason FROM authored_spans WHERE candidate_id=?", (row["id"],))
        view["authored"] = bool(authored)
        view["author_reason"] = authored[0] if authored else None
        if detail:
            view["span"] = self._span_view(row["attempt_id"], row["span_id"], candidate_id=row["id"])
            view["decisions"] = [{"id": d["id"], "n": d["n"], "action": d["action"], "reason": d["reason"],
                                  "created_at": d["created_at"], "claim_revision_id": d["claim_revision_id"],
                                  "body": jload(d["body_json"])} for d in decisions]
            view["related"] = self.related(row["id"])
        return view

    def list_candidates(self, *, source_id=None, status=None, query=None, limit=200, offset=0) -> dict:
        sql = ("SELECT c.* FROM candidates c JOIN sources s ON s.id=c.source_id "
               "WHERE s.current_attempt=c.attempt_id")
        args: list = []
        if source_id:
            sql += " AND c.source_id=?"
            args.append(source_id)
        if query:
            sql += " AND c.fact_key LIKE ?"
            args.append("%" + query.replace("%", "") + "%")
        sql += " ORDER BY s.created_seq, c.ord"
        rows = self.store.q(sql, args)
        views = [self._candidate_view(r) for r in rows]
        if status:
            views = [v for v in views if v["status"] == status]
        return {"total": len(views), "offset": offset, "items": views[offset:offset + limit]}

    def get_candidate(self, candidate_id: str) -> dict:
        row = self.store.q1("SELECT * FROM candidates WHERE id=?", (candidate_id,))
        if not row:
            raise ServiceError(404, "candidate_not_found")
        return self._candidate_view(row, detail=True)

    def related(self, candidate_id: str) -> list[dict]:
        """Reconciliation proposals only; nothing is merged or deleted (chapter 17)."""
        row = self.store.q1("SELECT * FROM candidates WHERE id=?", (candidate_id,))
        out = []
        rows = self.store.q("SELECT c.* FROM candidates c JOIN sources s ON s.id=c.source_id "
                            "WHERE c.fact_key=? AND c.id<>? AND s.current_attempt=c.attempt_id",
                            (row["fact_key"], candidate_id))
        mine_pred = jload(row["predicate_json"])
        if mine_pred["predicate"] != "fact.equals":
            return []
        mine = mine_pred["arguments"]["expected"]
        for r in rows:
            other_pred = jload(r["predicate_json"])
            if other_pred["predicate"] != "fact.equals":
                continue
            other = other_pred["arguments"]["expected"]
            eq = V.compare_equal(mine, other)
            kind = "DUPLICATE" if eq else "POSSIBLE_CONTRADICTION" if eq is False else "NEEDS_REVIEW"
            src = self._source_row(r["source_id"])
            out.append({"relation": kind, "candidate_id": r["id"], "source_name": src["display_name"],
                        "source_id": r["source_id"], "value": other,
                        "note": ("no effective intervals or units were extracted; a human must decide whether "
                                 "these statements apply to the same scope and time") if kind != "DUPLICATE" else
                                "exact predicate equality only; scope not verified"})
        return out

    def reconciliation(self) -> list[dict]:
        seen, out = set(), []
        for r in self.store.q("SELECT c.id FROM candidates c JOIN sources s ON s.id=c.source_id WHERE s.current_attempt=c.attempt_id ORDER BY c.fact_key, c.id"):
            for rel in self.related(r["id"]):
                pair = tuple(sorted((r["id"], rel["candidate_id"])))
                if pair in seen:
                    continue
                seen.add(pair)
                out.append({"a": r["id"], "b": rel["candidate_id"], "relation": rel["relation"],
                            "note": rel["note"]})
        return out

    # ----------------------------------------------------------------- decisions
    def decide(self, candidate_id: str, action: str, if_match: str | None, *, edit: dict | None = None,
               reason: str | None = None, idempotency_key: str | None = None) -> dict:
        if action not in ACTIONS:
            raise ServiceError(422, "bad_action", action)
        payload = {"action": action, "edit": edit, "reason": reason, "if_match": if_match}
        scope = "decide:" + candidate_id
        prior = self._idem(scope, idempotency_key, payload)
        if prior is not None:
            return prior
        reason = (reason or "").strip() or None
        if action in ("REJECT", "APPROVE_WITH_EDIT") and not reason:
            raise ServiceError(422, "reason_required", "a reason is required for this action")
        with self.store.tx() as db:
            cand = db.execute("SELECT * FROM candidates WHERE id=?", (candidate_id,)).fetchone()
            if not cand:
                raise ServiceError(404, "candidate_not_found")
            n = db.execute("SELECT COUNT(*) FROM decisions WHERE candidate_id=?", (candidate_id,)).fetchone()[0]
            if if_match is None:
                raise ServiceError(428, "precondition_required", "If-Match is required", etag=f'"c{n}"')
            if if_match != f'"c{n}"':
                raise ServiceError(412, "precondition_failed", "the candidate changed; reload and compare",
                                   etag=f'"c{n}"')
            src = db.execute("SELECT * FROM sources WHERE id=?", (cand["source_id"],)).fetchone()
            if src["current_attempt"] != cand["attempt_id"]:
                raise ServiceError(409, "candidate_superseded", "this candidate belongs to a superseded extraction")
            pred = jload(cand["predicate_json"])
            final = {"predicate": pred["predicate"], "subject": pred["subject"], "arguments": pred["arguments"]}
            if action == "APPROVE_WITH_EDIT":
                if not isinstance(edit, dict):
                    raise ServiceError(422, "edit_required")
                final = self._validate_edit(final, edit)
            claim_rev_id = None
            decision_id = record_id("decision", {"candidate_id": candidate_id, "n": n + 1, "action": action,
                                                 "reason": reason, "final": final if action in APPROVE else None,
                                                 "edit": edit})
            if action in APPROVE:
                rev = db.execute("SELECT COALESCE(MAX(rev),0)+1 FROM claim_revisions WHERE candidate_id=?", (candidate_id,)).fetchone()[0]
                body = {"candidate_id": candidate_id, "rev": rev, "decision_id": decision_id,
                        "predicate": final["predicate"], "subject": final["subject"], "arguments": final["arguments"],
                        "origin": {"source_id": cand["source_id"], "attempt_id": cand["attempt_id"],
                                   "span_id": cand["span_id"]},
                        "qualifiers": jload(cand["qualifiers_json"]),
                        "original": pred if action == "APPROVE_WITH_EDIT" else None}
                claim_rev_id = record_id("claimrev", body)
                db.execute("UPDATE claim_revisions SET status='SUPERSEDED' WHERE candidate_id=? AND status='CURRENT'", (candidate_id,))
                db.execute("INSERT INTO claim_revisions VALUES(?,?,?,?,?,?,?,?)",
                           (claim_rev_id, candidate_id, rev, decision_id, jdump(body), "CURRENT",
                            self.store.next_seq("claim"), now_iso()))
            elif action == "REJECT":
                db.execute("UPDATE claim_revisions SET status='RETRACTED' WHERE candidate_id=? AND status='CURRENT'", (candidate_id,))
            db.execute("INSERT INTO decisions VALUES(?,?,?,?,?,?,?,?,?)",
                       (decision_id, candidate_id, n + 1, action, reason, jdump({"final": final, "edit": edit}),
                        claim_rev_id, self.store.next_seq("decision"), now_iso()))
        out = self.get_candidate(candidate_id)
        self._idem_store(scope, idempotency_key, payload, out)
        return out

    def _validate_edit(self, base: dict, edit: dict) -> dict:
        allowed = {"predicate", "subject", "arguments"}
        if set(edit) - allowed:
            raise ServiceError(422, "bad_edit", "unknown edit fields")
        final = {**base, **{k: v for k, v in edit.items() if k in allowed}}
        if final == base:
            raise ServiceError(422, "edit_without_change", "the edit does not change the interpretation")
        rule = next((r for r in map(lambda k: self.registry.get(*k), self.registry.keys())
                     if r.predicate == final["predicate"]), None)
        if rule is None:
            raise ServiceError(422, "unknown_predicate", str(final["predicate"]))
        if not isinstance(final["subject"], str) or not final["subject"].strip() or len(final["subject"]) > 500:
            raise ServiceError(422, "bad_subject")
        if not isinstance(final["arguments"], dict):
            raise ServiceError(422, "bad_arguments")
        try:
            canonical_bytes(final["arguments"])
        except Exception as exc:
            raise ServiceError(422, "bad_arguments", str(exc))
        err = rule.validate_args(final["arguments"])
        if err:
            raise ServiceError(422, "bad_arguments", err)
        probe = Claim.make(final["predicate"], final["subject"], final["arguments"], [], [])
        if rule._applicability(probe) is False:
            raise ServiceError(422, "bad_arguments", "applicability needs decision, rationale and scope")
        return final

    # --------------------------------------------------------------- claim views
    def list_claims(self) -> list[dict]:
        out = []
        for r in self.store.q("SELECT * FROM claim_revisions ORDER BY created_seq DESC"):
            body = jload(r["body_json"])
            out.append({"id": r["id"], "candidate_id": r["candidate_id"], "rev": r["rev"], "status": r["status"],
                        "predicate": body["predicate"], "subject": body["subject"], "arguments": body["arguments"],
                        "qualifiers": body["qualifiers"], "origin_source_id": body["origin"]["source_id"]})
        return out

    # ------------------------------------------------------------------- runs
    # ------------------------------------------------------------------ risk packs
    def list_packs(self) -> list[dict]:
        return [{"pack_id": p.pack_id, "version": p.version, "digest": p.content_digest, "title": p.body["title"],
                 "description": p.body.get("description", ""), "obligations": len(p.obligations),
                 "requires": p.body.get("requires", []), "source_refs": p.body["source_refs"],
                 "applicability_facts": sorted({f for o in p.obligations for f in _pred_facts(o.get("applicability"))}
                                               | set(_pred_facts(p.body["applicability"])))}
                for p in self.packs.list()]

    def install_pack(self, data: bytes) -> dict:
        try:
            pack = self.packs.install(data)
        except RP.PackError as exc:
            raise ServiceError(422, "pack_" + exc.code, str(exc))
        return next(p for p in self.list_packs() if p["pack_id"] == pack.pack_id and p["version"] == pack.version)

    # ------------------------------------------------------------------ purpose profiles
    def list_purposes(self) -> list[dict]:
        return [{"profile_id": p.profile_id, "version": p.version, "digest": p.content_digest, "title": p.body["title"],
                 "description": p.body.get("description", ""),
                 "roles": [{"role": r["role"], "description": r.get("description", ""), "min": r.get("min", 1), "max": r.get("max", 1),
                            "value_kind": r.get("value_kind", "any"), "optional": bool(r.get("optional"))} for r in p.roles],
                 "coherence": p.body.get("coherence", []), "lifecycle": p.body.get("lifecycle")} for p in self.purposes.list()]

    def install_purpose(self, data: bytes) -> dict:
        try:
            p = self.purposes.install(data)
        except PU.PurposeError as exc:
            raise ServiceError(422, "purpose_" + exc.code, str(exc))
        return next(x for x in self.list_purposes() if x["profile_id"] == p.profile_id and x["version"] == p.version)

    def start_run(self, claim_revision_ids: list[str], evidence_source_ids: list[str],
                  idempotency_key: str | None = None, pack_ids: list[str] | None = None,
                  context: dict | None = None, purposes: list[dict] | None = None) -> dict:
        payload = {"claims": sorted(claim_revision_ids), "evidence": sorted(evidence_source_ids),
                   "packs": sorted(pack_ids or []), "context": context or {}, "purposes": purposes or []}
        prior = self._idem("run", idempotency_key, payload)
        if prior is not None:
            return prior
        if not evidence_source_ids or not (claim_revision_ids or purposes):
            raise ServiceError(422, "empty_selection", "choose evidence and at least one claim revision or purpose")
        revs = []
        for rid in sorted(set(claim_revision_ids)):
            row = self.store.q1("SELECT * FROM claim_revisions WHERE id=?", (rid,))
            if not row:
                raise ServiceError(404, "claim_revision_not_found", rid)
            if row["status"] != "CURRENT":
                raise ServiceError(409, "claim_revision_not_current", rid)
            revs.append(row)
        evidence = {}
        for sid in sorted(set(evidence_source_ids)):
            src = self._source_row(sid)
            att = self.store.q1("SELECT * FROM attempts WHERE id=?", (src["current_attempt"],)) if src["current_attempt"] else None
            if not att or att["state"] != "EXTRACTED":
                raise ServiceError(409, "evidence_not_ready", f"{src['display_name']} has no completed extraction")
            if src["superseded_by"]:
                raise ServiceError(409, "evidence_superseded", f"{src['display_name']} was replaced by a newer import")
            evidence[sid] = att["id"]
        try:
            packs = self.packs.resolve(list(pack_ids or []))
            facts = RP.check_facts(context or {})
        except RP.PackError as exc:
            raise ServiceError(422, "pack_" + exc.code, str(exc))
        purpose_reqs = []
        try:
            for req in purposes or []:
                prof = self.purposes.get(req["profile_id"], req.get("version"))
                purpose_reqs.append((prof, PU.check_bindings(prof, req.get("bindings", {}))))
        except (PU.PurposeError, KeyError, TypeError) as exc:
            raise ServiceError(422, "purpose_" + getattr(exc, "code", "bad_request"), str(exc))
        deps = {"claim_revisions": [r["id"] for r in revs], "evidence": evidence,
                "packs": {p.pack_id: p.content_digest for p in packs}, "context_digest": RP.context_digest(facts),
                "purposes": [{"ref": pr.ref(), "bindings_digest": sha256_digest(canonical_bytes(b))} for pr, b in purpose_reqs],
                "origins": {r["id"]: jload(r["body_json"])["origin"]["attempt_id"] for r in revs},
                "profile_digest": self.registry.profile_digest()}
        seq = self.store.next_seq("run")
        run_id = record_id("run", {"deps": deps, "seq": seq})
        self.store.x("INSERT INTO runs(id,job_id,state,input_json,deps_json,created_seq,created_at) VALUES(?,?,?,?,?,?,?)",
                     (run_id, "", "QUEUED", jdump(payload), jdump(deps), seq, now_iso()))
        job_id = self._submit("run", {"run_id": run_id}, lambda ctx: self._run_job_fn(ctx, run_id, revs, evidence, packs, facts, purpose_reqs))
        self.store.x("UPDATE runs SET job_id=? WHERE id=?", (job_id, run_id))
        out = {"job_id": job_id, "run_id": run_id, "events_url": f"/api/v1/jobs/{job_id}/events"}
        self._idem_store("run", idempotency_key, payload, out)
        return out

    def _compile(self, revs, evidence: dict[str, str], packs=(), facts=None, purpose_reqs=()):
        blobs: dict[str, bytes] = {}
        artifacts: dict[str, Artifact] = {}
        activities: dict[str, Activity] = {}
        record_cache: dict[str, tuple[dict, Artifact, str]] = {}

        def add_blob(data: bytes, media: str) -> Artifact:
            d = sha256_digest(data)
            art = Artifact.make(d, media, len(data), "artifacts/" + d.split(":", 1)[1])
            blobs[d] = data
            artifacts[art.id] = art
            return art

        def source_bundle(source_id: str, attempt_id: str):
            if attempt_id in record_cache:
                return record_cache[attempt_id]
            src = self._source_row(source_id)
            att = self.store.q1("SELECT * FROM attempts WHERE id=?", (attempt_id,))
            rec = self._record_for_attempt(att)
            src_art = add_blob(read_blob(self.store.blobs, src["digest"]), MEDIA[src["detected_format"]])
            outputs = []
            if not rec["rendition_is_source"]:
                outputs.append(add_blob(read_blob(self.store.blobs, rec["rendition_digest"]),
                                        "text/plain; charset=utf-8").id)
            rec_art = add_blob(read_blob(self.store.blobs, att["record_digest"]), EXTRACTION_MEDIA)
            outputs.append(rec_art.id)
            act = Activity.make(EXTRACT_OP, [src_art.id], outputs, [], rec["manifest"]["id"])
            activities[act.id] = act
            record_cache[attempt_id] = (rec, src_art, rec_art.id)
            return record_cache[attempt_id]

        for sid, aid in evidence.items():
            source_bundle(sid, aid)
        evidence_artifact_ids = [record_cache[aid][1].id for aid in evidence.values()]
        claims, assumptions, claim_index = [], {}, []
        for row in revs:
            body = jload(row["body_json"])
            origin = body["origin"]
            rec, src_art, _ = source_bundle(origin["source_id"], origin["attempt_id"])
            dec = self.store.q1("SELECT * FROM decisions WHERE id=?", (body["decision_id"],))
            fact = self._origin_fact(rec, origin["attempt_id"], origin["span_id"], body["candidate_id"])
            rend = read_blob(self.store.blobs, rec["rendition_digest"])
            loc = fact["locator"]
            asm_ids = []
            for q in body["qualifiers"]:
                a = Assumption.make(f"extraction qualifier acknowledged by reviewer: {q}", row["id"])
                assumptions[a.id] = a
                asm_ids.append(a.id)
            prov = {"schema": "verifier-lab/claim-provenance/1", "claim_revision_id": row["id"],
                    "candidate_id": body["candidate_id"], "origin_source_artifact_id": src_art.id,
                    "origin_span": {"span_id": origin["span_id"], "rendition_digest": rec["rendition_digest"],
                                    "locator": loc, "text_digest": sha256_digest(rend[loc["byte_start"]:loc["byte_end"]]),
                                    "grammar": fact["grammar"], "key": fact["key"], "value": fact["value"],
                                    "extraction_manifest_id": rec["manifest"]["id"]},
                    "decision": {"id": dec["id"], "action": dec["action"], "reason": dec["reason"],
                                 "edited": body["original"] is not None, "original": body["original"]},
                    "interpretation_note": "approval records what to test, not that the statement is true"}
            authored = self.store.q1("SELECT reason FROM authored_spans WHERE candidate_id=?", (body["candidate_id"],))
            if authored:
                prov["authoring"] = {"method": "human-selected-text", "reason": authored[0],
                                     "semantic_entailment": "not_machine_verified"}
            prov_art = add_blob(canonical_bytes(prov), PROVENANCE_MEDIA)
            claim = Claim.make(body["predicate"], body["subject"], body["arguments"],
                               [prov_art.id] + evidence_artifact_ids, asm_ids)
            claims.append(claim)
            claim_index.append({"claim_id": claim.id, "claim_revision_id": row["id"],
                                "candidate_id": body["candidate_id"], "subject": claim.subject,
                                "predicate": claim.predicate})
        profile = ProfileRef(PROFILE_ID, PROFILE_VERSION, self.registry.profile_digest())
        profiles = [profile]
        if packs:
            for pk in packs:     # pinned by exact content digest; the pack bytes travel inside the bundle
                add_blob(canonical_bytes(pk.body), RISKPACK_MEDIA)
                profiles.append(ProfileRef(pk.pack_id, pk.version, pk.content_digest))
            add_blob(canonical_bytes(facts or {}), CONTEXT_MEDIA)
        ev_sources = []
        for sid, aid in evidence.items():
            rec, src_art, _ = record_cache[aid]
            names = self._source_row(sid)["display_name"]
            rend_art = next((a for a in artifacts.values() if a.digest == rec["rendition_digest"]), None)
            ev_sources.append({"artifact_id": src_art.id, "name": names, "coverage": rec["coverage"]["summary"], "facts": rec["facts"],
                               "blocked_keys": rec["blocked_keys"], "rendition_artifact_id": rend_art.id if rend_art else None})
        for prof, binds in purpose_reqs:
            add_blob(canonical_bytes(prof.body), PURPOSE_MEDIA)
            add_blob(canonical_bytes({"profile": prof.ref(), "bindings": binds,
                                      "evidence_sources": sorted(e["artifact_id"] for e in ev_sources)}), PURPOSE_BINDINGS_MEDIA)
            profiles.append(ProfileRef(prof.profile_id, prof.version, prof.content_digest))
        bundle = Bundle.build(artifacts.values(), claims, activities.values(), assumptions.values(), profiles)
        return bundle, blobs, claim_index, ev_sources

    def _run_job_fn(self, ctx: JobContext, run_id: str, revs, evidence: dict[str, str], packs=(), facts=None, purpose_reqs=()) -> dict:
        started = time.monotonic()
        self.store.x("UPDATE runs SET state='RUNNING' WHERE id=?", (run_id,))
        ctx.progress("compiling")
        bundle, blobs, claim_index, ev_sources = self._compile(revs, evidence, packs, facts, purpose_reqs)

        def reader(art: Artifact) -> bytes:
            data = blobs[art.digest]
            if sha256_digest(data) != art.digest or len(data) != art.size_bytes:
                raise ValueError("artifact digest mismatch")
            return data
        context = CheckContext(bundle, reader)
        plan = CheckPlan.from_registry(self.registry)

        def progress(done, total, claim):
            ctx.progress("evaluating", done, total)
            hook = self.hooks.get("after_claim")
            if hook:
                hook(done, total, claim)
        ctx.progress("evaluating", 0, len(bundle.claims))
        run = check_claims(context, bundle.claims, plan, self.registry, ctx.cancelled, progress)
        if run.cancelled:
            self.store.x("UPDATE runs SET state='CANCELLED' WHERE id=?", (run_id,))
            raise JobCancelled()
        if run.errors:
            self.store.x("UPDATE runs SET state='FAILED', summary_json=? WHERE id=?",
                         (jdump({"errors": [{"claim_id": c, **e.to_dict()} for c, e in run.errors]}), run_id))
            raise ExtractionFailure("rule_execution_errors", f"{len(run.errors)} rule invocation(s) failed")
        ctx.progress("exporting")
        result = {
            "schema": "verifier-lab/result/1", "bundle_id": bundle.bundle_id,
            "plan": [list(x) for x in plan.predicate_rules],
            "profile": {"id": PROFILE_ID, "version": PROFILE_VERSION, "digest": self.registry.profile_digest()},
            "findings": [f.to_dict() for f in run.findings],
            "errors": [{"claim_id": c, "error": e.to_dict()} for c, e in run.errors],
            "claims": sorted(claim_index, key=lambda c: c["claim_id"]),
            "graph": inspect_bundle(bundle, frozenset(
                i for act in bundle.activities if act.operation == EXTRACT_OP for i in act.inputs)).to_dict(),
            "completion": {"cancelled": False, "claims_total": run.total_claims,
                           "claims_completed": run.completed_claims},
            "limitations": LIMITATIONS}
        if packs:
            by_claim = {f.claim_id: f.to_dict() for f in run.findings}
            cl = [{"id": c["claim_id"], "predicate": c["predicate"], "subject": c["subject"]} for c in claim_index]
            result["risk_packs"] = [pk.profile_ref() for pk in packs]
            result["context_digest"] = RP.context_digest(facts or {})
            result["obligations"] = [e for pk in packs for e in RP.plan_and_assess(pk, facts or {}, cl, by_claim)]
            result["limitations"] = LIMITATIONS + [
                "Risk-pack results are evidence assessments under the declared pack, context facts and rules; "
                "they are not legal, safety or compliance certification."]
        if purpose_reqs:
            result["purposes"] = [PU.evaluate_purpose(pr, b, ev_sources) for pr, b in purpose_reqs]
            result["limitations"] = result["limitations"] + [
                "A purpose assessment states what the supplied evidence suffices for; it is never a claim that a package is simply 'complete'."]
        meta = {"tool": "verifier-lab", "tool_version": __version__, "runtime": runtime_identity(),
                "elapsed_seconds": round(time.monotonic() - started, 3), "created_at": now_iso(),
                "note": "observation metadata; not part of the semantic result identity"}
        dest = self.store.root / "runs" / run_id.replace(":", "_")
        write_package(dest, bundle, blobs, result, meta, render_report(result, result["claims"]))
        counts = Counter(f.verdict.value for f in run.findings)
        self.store.x("UPDATE runs SET state='SUCCEEDED', summary_json=?, package_dir=?, bundle_id=? WHERE id=?",
                     (jdump({"counts": dict(counts), "claims": run.total_claims}), str(dest), bundle.bundle_id, run_id))
        return {"run_id": run_id, "bundle_id": bundle.bundle_id}

    def _freshness(self, row) -> dict:
        if row["state"] != "SUCCEEDED":
            return {"state": "UNKNOWN", "causes": []}
        deps, causes = jload(row["deps_json"]), []
        for rid in deps["claim_revisions"]:
            cr = self.store.q1("SELECT status FROM claim_revisions WHERE id=?", (rid,))
            if not cr or cr["status"] != "CURRENT":
                causes.append({"kind": "claim_revision_" + (cr["status"].lower() if cr else "missing"), "id": rid})
        for sid, aid in deps["evidence"].items():
            s = self.store.q1("SELECT current_attempt, superseded_by FROM sources WHERE id=?", (sid,))
            if not s or s["current_attempt"] != aid:
                causes.append({"kind": "evidence_reextracted", "id": sid})
            elif s["superseded_by"]:
                causes.append({"kind": "evidence_superseded", "id": sid, "by": s["superseded_by"]})
        for rid, aid in deps["origins"].items():
            cand = self.store.q1("SELECT source_id FROM claim_revisions cr JOIN candidates c ON c.id=cr.candidate_id WHERE cr.id=?", (rid,))
            if cand:
                s = self.store.q1("SELECT current_attempt, superseded_by FROM sources WHERE id=?", (cand["source_id"],))
                if s and s["current_attempt"] != aid:
                    causes.append({"kind": "origin_reextracted", "id": cand["source_id"]})
                elif s and s["superseded_by"]:
                    causes.append({"kind": "origin_superseded", "id": cand["source_id"], "by": s["superseded_by"]})
        for pr in deps.get("purposes", []):
            if not any(p.content_digest == pr["ref"]["digest"] for p in self.purposes.list()):
                causes.append({"kind": "purpose_profile_no_longer_installed", "id": pr["ref"]["profile_id"]})
        for pid, digest in deps.get("packs", {}).items():
            try:
                if self.packs.get(pid).content_digest != digest and not any(
                        p.content_digest == digest for p in self.packs.list() if p.pack_id == pid):
                    causes.append({"kind": "pack_no_longer_installed", "id": pid})
            except RP.PackError:
                causes.append({"kind": "pack_no_longer_installed", "id": pid})
        if deps["profile_digest"] != self.registry.profile_digest():
            causes.append({"kind": "rule_profile_changed", "id": PROFILE_ID})
        return {"state": "STALE" if causes else "CURRENT", "causes": causes}

    def get_run(self, run_id: str, include_findings: bool = True) -> dict:
        row = self.store.q1("SELECT * FROM runs WHERE id=?", (run_id,))
        if not row:
            raise ServiceError(404, "run_not_found")
        job = self.get_job(row["job_id"]) if row["job_id"] else None
        view = {"id": row["id"], "state": row["state"], "job": job, "created_at": row["created_at"],
                "inputs": jload(row["input_json"]), "freshness": self._freshness(row),
                "bundle_id": row["bundle_id"], "summary": jload(row["summary_json"]), "findings": [], "errors": []}
        if row["state"] == "SUCCEEDED" and include_findings:
            result = json.loads((Path(row["package_dir"]) / "result.json").read_bytes())
            by_claim = {c["claim_id"]: c for c in result["claims"]}
            bundle = json.loads((Path(row["package_dir"]) / "bundle.json").read_bytes())
            digest_of = {a["id"]: a for a in bundle["artifacts"]}
            names = {r["digest"]: r["display_name"] for r in self.store.q("SELECT digest, display_name FROM sources")}
            view["findings"] = [{**f, "claim": by_claim.get(f["claim_id"]),
                                 "evidence_view": self._evidence_view(f, digest_of, names)} for f in result["findings"]]
            view["errors"] = result["errors"]
            view["completion"] = result["completion"]
            view["obligations"] = result.get("obligations", [])
            view["purposes"] = [{**p, "newer_version": self._newer_purpose(p["profile"])} for p in result.get("purposes", [])]
            view["risk_packs"] = result.get("risk_packs", [])
            view["context"] = json.loads(bundle_context) if (bundle_context := self._context_text(row)) else {}
        elif row["state"] == "FAILED":
            view["errors"] = (jload(row["summary_json"]) or {}).get("errors", [])
        return view

    def _newer_purpose(self, ref: dict):
        try:
            return self.purposes.newer_than(ref["profile_id"], ref["profile_version"])
        except PU.PurposeError:
            return None

    def _context_text(self, row) -> str | None:
        """Context facts are stored as an artifact in the pinned package."""
        pkg = Path(row["package_dir"])
        bundle = json.loads((pkg / "bundle.json").read_bytes())
        for a in bundle["artifacts"]:
            if a["media_type"] == CONTEXT_MEDIA:
                return (pkg / a["relative_path"]).read_text()
        return None

    def _evidence_view(self, finding: dict, digest_of: dict, names: dict, context: int = 160) -> list[dict]:
        """Exact cited tokens with surrounding text, read from the immutable renditions."""
        out = []
        for o in ((finding.get("witness") or {}).get("observations") or [])[:20]:
            art = digest_of.get(o.get("rendition_artifact_id") or o["artifact_id"])
            src = digest_of.get(o["artifact_id"])
            if not art:
                continue
            data = read_blob(self.store.blobs, art["digest"])
            a, b = o["byte_start"], o["byte_end"]
            dec = lambda x: x.decode("utf-8", errors="replace")
            out.append({"source_name": names.get(src["digest"], "source") if src else "source",
                        "key": o["key"], "value": o["value"], "before": dec(data[max(0, a - context):a]),
                        "token": dec(data[a:b]), "after": dec(data[b:b + context]),
                        "qualifiers": o.get("qualifiers", [])})
        return out

    def list_runs(self) -> list[dict]:
        return [self.get_run(r["id"], include_findings=False)
                for r in self.store.q("SELECT id FROM runs ORDER BY created_seq DESC")]

    def export_run(self, run_id: str) -> Path:
        row = self.store.q1("SELECT * FROM runs WHERE id=?", (run_id,))
        if not row:
            raise ServiceError(404, "run_not_found")
        if row["state"] != "SUCCEEDED":
            raise ServiceError(409, "run_not_exportable", "only completed runs can be exported")
        data = zip_package(Path(row["package_dir"]))
        out = self.store.root / "exports" / (run_id.replace(":", "_") + ".zip")
        tmp = out.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(out)
        return out

    def verify_export(self, path: Path) -> dict:
        from .checker import verify_package
        return verify_package(Path(path))

    def overview(self) -> dict:
        sources = self.list_sources()
        runs = self.list_runs()
        counts = Counter()
        for r in self.store.q("SELECT c.id FROM candidates c JOIN sources s ON s.id=c.source_id WHERE s.current_attempt=c.attempt_id"):
            counts[self._candidate_status(r["id"])] += 1
        latest = next((r for r in runs if r["state"] == "SUCCEEDED"), None)
        verdicts = {}
        if latest:
            verdicts = (latest["summary"] or {}).get("counts", {})
        return {"sources": len(sources), "candidates": dict(counts), "claims": len([c for c in self.list_claims() if c["status"] == "CURRENT"]),
                "latest_run": None if not latest else {"id": latest["id"], "freshness": latest["freshness"], "counts": verdicts},
                "capabilities": self.capabilities()}

    # --------------------------------------------------------- advanced: experiments (bounded, synchronous)
    def experiment_laboratory(self, system, seed="seed-1", ops=60, boundary=None, defects=None,
                             reduce=False, max_oracle_calls=3000):
        import tempfile
        from .experiments.laboratory import generated_case, reduce_case, run_case
        from .experiments.package import verify_package_fresh_process, write_package
        try:
            case = generated_case(system, seed, ops, boundary, () if defects is None else defects)
            if not reduce:
                return {"case": case.identity(), **run_case(case).to_dict()}
            summary, package = reduce_case(case, max_oracle_calls)
        except (TypeError, ValueError) as exc:
            raise ServiceError(422, "bad_laboratory_case", str(exc)) from exc
        if package is None:
            return summary
        with tempfile.TemporaryDirectory(prefix="vl-laboratory-") as directory:
            path = Path(directory) / "counterexample.json"
            write_package(package, path)
            checked = verify_package_fresh_process(path)
        return {**summary, "fresh_process_verification": checked["status"], "package": package}

    def _case(self, seed, ops, boundary, defects):
        from .experiments.checkpoint import ExperimentCase
        from .experiments.history import generate_history
        from .subjects.candidate_store import DEFECTS
        if not isinstance(seed, str) or not seed or len(seed) > 100:
            raise ServiceError(422, "bad_seed")
        if not isinstance(ops, int) or isinstance(ops, bool) or not 0 <= ops <= 400:
            raise ServiceError(422, "bad_ops", "ops must be an integer between 0 and 400")
        bad = [d for d in (defects or []) if d not in DEFECTS]
        if bad:
            raise ServiceError(422, "unknown_defect", f"{bad}; known: {list(DEFECTS)}")
        b = ops // 2 if boundary is None else boundary
        return ExperimentCase(seed, tuple(generate_history(seed, ops)), b, tuple(defects or []))

    def experiment_checkpoint(self, seed="seed-1", ops=60, boundary=None, defects=None) -> dict:
        from .experiments.checkpoint import run_experiment
        case = self._case(seed, ops, boundary, defects)
        return {"case": case.identity(), "defects": list(case.defects), **run_experiment(case).to_dict()}

    def experiment_crash(self, writer_defect=None, recovery_defects=None, max_positions=None) -> dict:
        from .experiments import crash
        if writer_defect not in (None, *crash.WRITER_DEFECTS) or any(d not in crash.RECOVERY_DEFECTS for d in (recovery_defects or [])):
            raise ServiceError(422, "unknown_defect")
        return crash.crash_experiment(writer_defect, tuple(recovery_defects or ()), max_positions).to_dict()

    def experiment_minimize(self, seed="seed-1", ops=120, boundary=None, defects=None, max_oracle_calls=3000) -> dict:
        import tempfile
        from .experiments.package import CheckpointOracle, build_package, verify_package_fresh_process, write_package
        from .minimize import ReductionBudget, minimize
        from .reducers.sequence import DeleteTransactionMembers, DeleteUnits
        from .reducers.values import SimplifyValues
        if not defects:
            raise ServiceError(422, "defect_required", "inject at least one named defect to have a failure to reduce")
        case = self._case(seed, ops, boundary, defects)
        reducers = [DeleteUnits(), DeleteTransactionMembers(), SimplifyValues()]
        result = minimize(case, CheckpointOracle(), reducers, ReductionBudget(max_oracle_calls=min(int(max_oracle_calls), 3000)))
        if result.status in ("NOT_REPRODUCED", "UNSTABLE"):
            return {"status": result.status, "stop_reason": result.stop_reason, "original_units": len(case.ops)}
        pkg = build_package(result, reducers)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pkg.json"
            write_package(pkg, path)
            verification = verify_package_fresh_process(path)
        return {"status": result.status, "original_units": len(case.ops), "reduced_units": len(result.best.ops),
                "oracle_calls": result.oracle_calls, "scope": pkg["reduction"]["scope"], "reduced_case": pkg["reduced"]["case"],
                "signature": pkg["signature"], "witness": pkg["witness"],
                "fresh_process_verification": verification["status"], "package": pkg}

    # ------------------------------------------------------------------ advanced: composition analysis
    def _pipeline_of(self, source_id: str, subject: str):
        from . import composition as CP
        from .canonical import strict_loads
        src = self._source_row(source_id)
        if src["detected_format"] != "json":
            raise ServiceError(422, "not_a_json_source", "pipeline descriptions are JSON documents")
        try:
            doc = strict_loads(read_blob(self.store.blobs, src["digest"]), max_bytes=8_000_000, max_depth=24)
        except Exception as exc:
            raise ServiceError(422, "unreadable_pipeline_source", str(exc)[:200])
        if not isinstance(doc, dict) or subject not in doc:
            raise ServiceError(422, "pipeline_not_found", f"no '{subject}' object in {src['display_name']}")
        try:
            return src, CP.parse_pipeline(doc[subject])
        except CP.PipelineError as exc:
            raise ServiceError(422, "malformed_pipeline", str(exc))

    def composition_analyze(self, source_id: str, subject: str = "pipeline", target: str | None = None) -> dict:
        from . import composition as CP
        src, pipe = self._pipeline_of(source_id, subject)
        try:
            analysis = CP.analyze(pipe, target or None)
        except CP.PipelineError as exc:
            raise ServiceError(422, "bad_target", str(exc))
        depth: dict[str, int] = {}
        for node in analysis["order"]:
            depth[node] = max([depth[a] + 1 for a, b in analysis["edges"] if b == node and a in depth] or [0])
        acts = [{"id": a["id"], "depth": depth.get(a["id"]), "in_scope": a["id"] in analysis["scope"]["evaluated"],
                 "consumes": [{"artifact": c["artifact"], "requires": c["requires"]} for c in a["consumes"]],
                 "produces": [{"artifact": x["artifact"], "tags": x["tags"], "generation": x["generation"]} for x in a["produces"]],
                 "conversion": a["conversion"]} for a in pipe["activities"]]
        return {"source": {"id": source_id, "name": src["display_name"]}, "subject": subject, "target": target,
                "activities": acts, "external": pipe["external"], "same_generation": pipe["same_generation"], "analysis": analysis}

    def composition_impact(self, old_id: str, new_id: str, subject: str = "pipeline") -> dict:
        from . import composition as CP
        _, old = self._pipeline_of(old_id, subject)
        _, new = self._pipeline_of(new_id, subject)
        return CP.impact(old, new)

    # ------------------------------------------------------------------ advanced: mutation campaigns
    CAMPAIGN_KERNELS = {
        "real": "The production rules and engine",
        "always_supported": "Negative control: always answers SUPPORTED",
        "always_contradicted": "Negative control: always answers CONTRADICTED",
        "last_key_wins": "Negative control: silently resolves duplicate keys and conflicts",
        "self_reference_allowed": "Negative control: lets a claim's own source count as evidence"}

    def campaign_run(self, kernel: str = "real", seed: str = "campaign-1") -> dict:
        from . import campaign_checker
        from .harness import campaign
        if kernel not in self.CAMPAIGN_KERNELS:
            raise ServiceError(422, "unknown_kernel", f"known: {list(self.CAMPAIGN_KERNELS)}")
        if not isinstance(seed, str) or not 0 < len(seed) <= 100:
            raise ServiceError(422, "bad_seed")
        manifest = campaign.build_manifest(seed)
        report = campaign.run_campaign(kernel, manifest)
        check = campaign_checker.verify_campaign_report(report, manifest, manifest["manifest_digest"])
        return {"kernels": self.CAMPAIGN_KERNELS, "manifest": manifest, "report": report, "verification": check,
                "trusted_manifest_digest": manifest["manifest_digest"]}

    def campaign_verify(self, report, manifest, trusted_digest=None) -> dict:
        from . import campaign_checker
        if not isinstance(report, dict) or not isinstance(manifest, dict):
            raise ServiceError(422, "bad_campaign_documents")
        return campaign_checker.verify_campaign_report(report, manifest, trusted_digest)

    def capabilities(self) -> dict:
        from .ingestion.adapters import pdf_adapter
        ok, ver = pdf_adapter.available()
        return {"json": {"available": True}, "markdown": {"available": True},
                "sql": {"available": True, "dialects": ["generic", "postgresql", "mysql", "sqlite", "unresolved"]},
                "pdf": {"available": ok, "parser": "pypdf" if ok else None, "version": ver or None,
                        "ocr": False},
                "limits": self.limits.to_dict(), "max_upload_bytes": self.admission.max_bytes}


def _pred_facts(p) -> list[str]:
    if not p:
        return []
    if p["op"] in ("eq", "in", "exists"):
        return [p["fact"]]
    return [f for a in (p.get("args") or [p["arg"]] if "arg" in p else p["args"]) for f in _pred_facts(a)]

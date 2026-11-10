"""Application service shared by the HTTP API, the CLI and tests (one interpretation of results)."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
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
                 max_workers: int = 2, admission: AdmissionLimits | None = None, local_assistant=None):
        self.store = Store(Path(workspace))
        self.limits = limits or Limits()
        self.admission = admission or AdmissionLimits()
        self.registry = registry or builtin_registry()
        self.purposes = PU.PurposeCatalog(self.store.root / "purposes")
        self.packs = RP.PackCatalog(self.store.root / "packs", self.registry.implemented_predicates())
        self.executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="vl-job")
        self.hooks: dict = {}
        self.repository_snapshots: dict = {}
        self.repository_edges: dict = {}
        self.repository_scan_ids: dict = {}
        self.repository_content_access: dict = {}
        self.repository_assistant_permitted: dict = {}
        self.repository_roots: dict = {}
        self.repository_indexes: dict = {}
        self.repository_cache = None
        self.local_assistant = local_assistant
        self._recover()

    def close(self):
        self.executor.shutdown(wait=True, cancel_futures=False)
        if self.repository_cache is not None:
            self.repository_cache.close()
        self.store.close()

    def _scan_cache(self):
        """Opened on first use; a cache that cannot be opened only costs speed."""
        if self.repository_cache is None:
            from .repository_cache import ParseCache
            try:
                self.repository_cache = ParseCache(self.store.root / "scan-cache.db")
            except Exception:
                return None
        return self.repository_cache

    def repository_cache_status(self):
        cache = self._scan_cache()
        return cache.stats() if cache else {"records": 0, "bytes": 0}

    def repository_cache_clear(self):
        cache = self._scan_cache()
        if cache:
            cache.clear()
        return {"cleared": True}

    def repository_map(self, path: str, content_access: str = "internal", *, scan_limits=None,
                       progress=None, should_cancel=None, use_cache=False) -> dict:
        from .repository_map import ScanCancelled, scan_repository
        if not isinstance(path, str) or not path.strip() or len(path) > 4096:
            raise ServiceError(422, "bad_repository_path", "Enter a local directory path")
        if content_access not in ("internal", "adult", "adult_reviewed"):
            raise ServiceError(403, "adult_review_required")
        try:
            result = scan_repository(path, limits=self.limits, scan_limits=scan_limits, progress=progress,
                                     should_cancel=should_cancel, cache=self._scan_cache() if use_cache else None)
        except ScanCancelled as exc:
            raise ServiceError(409, "scan_cancelled", "The scan was stopped. Nothing from it was kept.") from exc
        except ExtractionFailure as exc:
            raise ServiceError(503, exc.code, str(exc)) from exc
        except (OSError, ValueError) as exc:
            raise ServiceError(422, "repository_unavailable", str(exc)) from exc
        try:
            canonical = Path(path).expanduser().absolute().resolve(strict=True)
        except OSError as exc:
            raise ServiceError(409, "stale_repository", "The repository location changed during the scan") from exc
        if hashlib.sha256(os.fsencode(str(canonical))).hexdigest() != result["repo_id"]:
            raise ServiceError(409, "stale_repository", "The repository location changed during the scan")
        with self.store.lock:
            location_id = result["repo_id"]
            location = self.store.q1("SELECT project_id FROM repository_locations WHERE location_id=?", (location_id,))
            if location:
                result["repo_id"] = location["project_id"]
                self.store.x("UPDATE repository_projects SET name=? WHERE project_id=? AND name LIKE 'Saved repository %'", (result["root_name"],result["repo_id"]))
            else:
                self.store.x("INSERT OR IGNORE INTO repository_projects(project_id,name) VALUES(?,?)", (location_id,result["root_name"]))
                self.store.x("INSERT INTO repository_locations(location_id,project_id) VALUES(?,?)", (location_id,location_id))
            result["location_id"] = location_id
            result["saved_projects"] = [{"id": r["project_id"], "name": r["name"], "labels": r["labels"], "progress": r["progress"]} for r in self.store.q(
                "SELECT p.project_id,p.name, "
                "(SELECT COUNT(*) FROM repository_labels l WHERE l.repo_id=p.project_id) AS labels, "
                "(SELECT COUNT(*) FROM repository_progress v WHERE v.repo_id=p.project_id) AS progress "
                "FROM repository_projects p WHERE p.project_id<>? AND "
                "(EXISTS(SELECT 1 FROM repository_labels l WHERE l.repo_id=p.project_id) OR "
                " EXISTS(SELECT 1 FROM repository_progress v WHERE v.repo_id=p.project_id)) ORDER BY p.name,p.project_id", (result["repo_id"],))]
            from .repository_index import RepositoryIndex
            nodes = {n["id"]: n for n in result["nodes"]}
            result["scan_id"] = secrets.token_urlsafe(18)
            self.repository_snapshots[result["repo_id"]] = nodes
            self.repository_edges[result["repo_id"]] = result["edges"]
            self.repository_indexes[result["repo_id"]] = RepositoryIndex(
                nodes, result["edges"], complete=not any(w.startswith("Connection limit reached") for w in result["warnings"]))
            self.repository_scan_ids[result["repo_id"]] = result["scan_id"]
            self.repository_content_access[result["repo_id"]] = content_access
            self.repository_roots[result["repo_id"]] = (location_id, str(canonical))
            assistant = self.local_assistant
            try:
                permitted = bool(content_access == "adult" and assistant and assistant.safe_for(str(canonical)))
                available = bool(permitted and assistant.available())
            except Exception:
                permitted, available = False, False
            # Bind approval to the configured provider checked for this scan. A
            # later configuration change must not inherit the old provider's check.
            self.repository_assistant_permitted[result["repo_id"]] = assistant if permitted else None
            result["local_assistant_available"] = bool(available and self.local_assistant is assistant)
            progress = {r["node_id"]: r["fingerprint"] for r in self.store.q(
                "SELECT node_id,fingerprint FROM repository_progress WHERE repo_id=?", (result["repo_id"],))}
            result["unmatched_progress_count"] = sum(node_id not in nodes for node_id in progress)
            for node in nodes.values():
                node["reviewed"] = node["id"] in progress
                current_digest = node.get("sha256") or node.get("fingerprint")
                node["review_needs_refresh"] = bool(node["reviewed"] and progress[node["id"]] and current_digest and progress[node["id"]] != current_digest)
            rows = self.store.q("SELECT l.node_id,l.label,a.kind,a.fingerprint,a.path,a.name "
                                "FROM repository_labels l LEFT JOIN repository_label_anchors a "
                                "ON a.repo_id=l.repo_id AND a.node_id=l.node_id WHERE l.repo_id=?", (result["repo_id"],))
            labels = {r["node_id"]: r["label"] for r in rows}
            by_id = {r["node_id"]: r for r in rows}
            for node in nodes.values():
                node["label"] = labels.get(node["id"], "")
                anchor = by_id.get(node["id"])
                node["label_needs_review"] = bool(anchor and anchor["fingerprint"] and node.get("fingerprint") and
                                                  anchor["fingerprint"] != node["fingerprint"])
            result["unmatched_labels"] = []
            by_fingerprint = None
            for row in rows:
                if row["node_id"] in nodes:
                    continue
                if by_fingerprint is None:  # built only when a saved label has lost its item
                    by_fingerprint = {}
                    for n in nodes.values():
                        if n.get("fingerprint"):
                            by_fingerprint.setdefault((n["fingerprint"], n["kind"]), []).append(n)
                matches = [n for n in by_fingerprint.get((row["fingerprint"], row["kind"]), []) if not n["label"]] if row["fingerprint"] else []
                result["unmatched_labels"].append({"node_id": row["node_id"], "label": row["label"],
                    "kind": row["kind"], "path": row["path"], "name": row["name"],
                    "suggested_node_id": matches[0]["id"] if len(matches) == 1 else None})
        return result

    def repository_release(self, repo_id, scan_id):
        """Drop a scan the caller no longer shows, so its memory is free before the next scan is built."""
        with self.store.lock:
            current = self.repository_scan_ids.get(repo_id) if isinstance(repo_id, str) else None
            if current is None or not isinstance(scan_id, str) or not secrets.compare_digest(current, scan_id):
                return {"released": False}
            for held in (self.repository_scan_ids, self.repository_snapshots, self.repository_edges, self.repository_indexes,
                         self.repository_content_access, self.repository_assistant_permitted, self.repository_roots):
                held.pop(repo_id, None)
        return {"released": True}

    def repository_remember(self, repo_id, scan_id):
        with self.store.tx() as db:
            self._check_repository_scan(repo_id, scan_id)
            if self.repository_content_access.get(repo_id) != "adult":
                raise ServiceError(403, "adult_session_required")
            location = self.repository_roots.get(repo_id)
            if not location:
                raise ServiceError(409, "stale_repository")
            db.execute("INSERT INTO repository_shelf(location_id,path) VALUES(?,?) "
                       "ON CONFLICT(location_id) DO UPDATE SET path=excluded.path", location)
        return {"remembered": True}

    def repository_shelf(self):
        with self.store.lock:
            rows = self.store.q("SELECT s.location_id,s.path,p.name FROM repository_shelf s "
                "JOIN repository_locations l ON l.location_id=s.location_id "
                "JOIN repository_projects p ON p.project_id=l.project_id ORDER BY p.name,s.path")
            return {"items": [{"location_id": r["location_id"], "path": r["path"], "name": r["name"]} for r in rows]}

    def repository_forget(self, location_id):
        if not isinstance(location_id, str) or len(location_id) != 64 or any(c not in "0123456789abcdef" for c in location_id):
            raise ServiceError(422, "bad_repository_request")
        with self.store.tx() as db:
            db.execute("DELETE FROM repository_shelf WHERE location_id=?", (location_id,))
        return {"forgotten": True}

    def repository_label(self, repo_id, node_id, label, scan_id):
        if (not isinstance(repo_id, str) or len(repo_id) != 64 or
                any(c not in "0123456789abcdef" for c in repo_id) or
                not isinstance(node_id, str) or not 0 < len(node_id) <= 2048 or
                not isinstance(label, str) or len(label) > 120 or any(ord(c) < 32 for c in label)):
            raise ServiceError(422, "bad_label", "Labels must be plain text, at most 120 characters")
        label = label.strip()
        with self.store.tx() as db:
            self._check_repository_scan(repo_id, scan_id)
            node = self.repository_snapshots.get(repo_id, {}).get(node_id)
            if not node:
                raise ServiceError(409, "stale_repository", "Scan this repository again before saving a label")
            if label:
                db.execute("INSERT INTO repository_labels(repo_id,node_id,label) VALUES(?,?,?) "
                           "ON CONFLICT(repo_id,node_id) DO UPDATE SET label=excluded.label", (repo_id,node_id,label))
                self._repository_anchor(db, repo_id, node)
            else:
                db.execute("DELETE FROM repository_labels WHERE repo_id=? AND node_id=?", (repo_id,node_id))
                db.execute("DELETE FROM repository_label_anchors WHERE repo_id=? AND node_id=?", (repo_id,node_id))
        return {"label": label}

    @staticmethod
    def _repository_anchor(db, repo_id, node):
        db.execute("INSERT INTO repository_label_anchors(repo_id,node_id,kind,fingerprint,path,name) VALUES(?,?,?,?,?,?) "
                   "ON CONFLICT(repo_id,node_id) DO UPDATE SET kind=excluded.kind,fingerprint=excluded.fingerprint,"
                   "path=excluded.path,name=excluded.name", (repo_id,node["id"],node["kind"],node.get("fingerprint"),node.get("path"),node.get("name")))

    def repository_rebind_label(self, repo_id, old_node_id, new_node_id, scan_id):
        if not all(isinstance(v, str) and 0 < len(v) <= 2048 for v in (repo_id, old_node_id, new_node_id)):
            raise ServiceError(422, "bad_label_request")
        with self.store.tx() as db:
            self._check_repository_scan(repo_id, scan_id)
            nodes = self.repository_snapshots.get(repo_id, {})
            target = nodes.get(new_node_id)
            old = db.execute("SELECT l.label,a.kind FROM repository_labels l LEFT JOIN repository_label_anchors a "
                             "ON a.repo_id=l.repo_id AND a.node_id=l.node_id WHERE l.repo_id=? AND l.node_id=?", (repo_id,old_node_id)).fetchone()
            existing = db.execute("SELECT label FROM repository_labels WHERE repo_id=? AND node_id=?", (repo_id,new_node_id)).fetchone()
            if not old or old_node_id in nodes or not target:
                raise ServiceError(409, "stale_repository", "Rescan and choose an unmatched label and current item")
            if existing or (old["kind"] and old["kind"] != target["kind"]):
                raise ServiceError(409, "label_conflict", "Choose an unlabeled item of the same kind")
            db.execute("INSERT INTO repository_labels(repo_id,node_id,label) VALUES(?,?,?)", (repo_id,new_node_id,old["label"]))
            self._repository_anchor(db, repo_id, target)
            db.execute("DELETE FROM repository_labels WHERE repo_id=? AND node_id=?", (repo_id,old_node_id))
            db.execute("DELETE FROM repository_label_anchors WHERE repo_id=? AND node_id=?", (repo_id,old_node_id))
        return {"label": old["label"]}

    def repository_reconnect(self, location_id, project_id, scan_id):
        if not all(isinstance(v, str) and len(v) == 64 and all(c in "0123456789abcdef" for c in v) for v in (location_id,project_id)):
            raise ServiceError(422, "bad_repository_request")
        with self.store.tx() as db:
            location = db.execute("SELECT project_id FROM repository_locations WHERE location_id=?", (location_id,)).fetchone()
            target = db.execute("SELECT project_id FROM repository_projects WHERE project_id=?", (project_id,)).fetchone()
            if not location or not target:
                raise ServiceError(409, "stale_repository", "Open the folder and select a saved repository again")
            current = location["project_id"]
            self._check_repository_scan(current, scan_id)
            if current == project_id:
                return {"project_id": project_id}
            if (db.execute("SELECT 1 FROM repository_labels WHERE repo_id=? LIMIT 1", (current,)).fetchone() or
                    db.execute("SELECT 1 FROM repository_progress WHERE repo_id=? LIMIT 1", (current,)).fetchone()):
                raise ServiceError(409, "label_conflict", "This folder already has labels or progress; automatic merging is not available")
            db.execute("UPDATE repository_locations SET project_id=? WHERE location_id=?", (project_id,location_id))
            self.repository_scan_ids.pop(current, None)
            self.repository_snapshots.pop(current, None)
            self.repository_edges.pop(current, None)
            self.repository_content_access.pop(current, None)
            self.repository_assistant_permitted.pop(current, None)
            self.repository_roots.pop(current, None)
            self.repository_indexes.pop(current, None)
        return {"project_id": project_id}

    def repository_practice(self, repo_id, node_id, answer, scan_id, activity="call"):
        activities = {"call": ("possible call", "source", "A possible call is inferred from source; it does not establish that the call ran."),
                      "definition": ("defines", "source", "A definition is present in the source; that alone does not show it executed."),
                      "callsite": ("contains written call", "source", "The call expression is written in source; that alone does not show it ran."),
                      "import": ("imports", "source", "An import or include is present in the source; it does not prove the dependency is installed or ran.")}
        if not all(isinstance(v, str) for v in (repo_id, node_id, answer, activity)) or answer not in ("source", "ran") or activity not in activities:
            raise ServiceError(422, "bad_practice_request")
        kind, correct, explanation = activities[activity]
        with self.store.lock:
            self._check_repository_scan(repo_id, scan_id)
            index = self.repository_indexes.get(repo_id)
            near = index.incident(node_id) if index else self.repository_edges.get(repo_id, [])
            edge = next((e for e in near if (e["kind"] == kind or (activity == "import" and e["kind"] == "includes")) and e["evidence"] == ("inferred" if activity == "call" else "static") and
                         (e["source"] == node_id if activity == "call" else e["target"] == node_id)), None)
        if not edge:
            raise ServiceError(409, "stale_repository", "Rescan and select an item with the relevant source evidence")
        return {"correct": answer == correct, "evidence": {k: edge.get(k) for k in ("path", "line", "snippet", "sha256")},
                "explanation": explanation, "scorer": "local-deterministic"}

    def _repository_reading_challenges(self, repo_id, node_id, level, scan_id):
        if not isinstance(node_id, str) or level not in ("beginner", "explore", "technical"):
            raise ServiceError(422, "bad_reading_practice")
        with self.store.lock:
            self._check_repository_scan(repo_id, scan_id)
            node = self.repository_snapshots.get(repo_id, {}).get(node_id)
            if not node:
                raise ServiceError(409, "stale_repository", "Choose an item in the current scan")
            from .repository_learning import reading_challenges
            return reading_challenges(node, level)

    def repository_reading_questions(self, repo_id, node_id, level, scan_id):
        questions = self._repository_reading_challenges(repo_id, node_id, level, scan_id)
        return [{key: question[key] for key in ("kind", "prompt", "choices", "evidence")} for question in questions]

    def repository_check_reading(self, repo_id, node_id, level, scan_id, kind, answer):
        if kind not in ("parameter", "return", "structure") or type(answer) is not int or not 0 <= answer < 2:
            raise ServiceError(422, "bad_reading_practice")
        questions = self._repository_reading_challenges(repo_id, node_id, level, scan_id)
        question = next((item for item in questions if item["kind"] == kind), None)
        if question is None:
            raise ServiceError(409, "practice_unavailable", "Choose a question from the current scan")
        return {"correct": answer == question["correct_index"], "evidence": question["evidence"],
                "explanation": question["explanation"]}

    def _check_repository_scan(self, repo_id, scan_id):
        if not isinstance(repo_id, str) or not isinstance(scan_id, str) or not scan_id.isascii() or len(scan_id) > 64:
            raise ServiceError(409, "stale_repository", "This repository was rescanned. Open it again to use the current evidence.")
        current = self.repository_scan_ids.get(repo_id)
        if current is None or not secrets.compare_digest(current, scan_id):
            raise ServiceError(409, "stale_repository", "This repository was rescanned. Open it again to use the current evidence.")

    def repository_progress(self, repo_id, node_id, reviewed, scan_id):
        if not isinstance(node_id, str) or type(reviewed) is not bool:
            raise ServiceError(422, "bad_progress_request")
        with self.store.tx() as db:
            self._check_repository_scan(repo_id, scan_id)
            node = self.repository_snapshots.get(repo_id, {}).get(node_id)
            if not node:
                raise ServiceError(409, "stale_repository", "Select an item in the current scan")
            if reviewed:
                db.execute("INSERT INTO repository_progress(repo_id,node_id,fingerprint) VALUES(?,?,?) "
                           "ON CONFLICT(repo_id,node_id) DO UPDATE SET fingerprint=excluded.fingerprint", (repo_id,node_id,node.get("sha256") or node.get("fingerprint")))
            else:
                db.execute("DELETE FROM repository_progress WHERE repo_id=? AND node_id=?", (repo_id,node_id))
        return {"reviewed": reviewed}

    def repository_progress_clear(self, repo_id, scan_id):
        with self.store.tx() as db:
            self._check_repository_scan(repo_id, scan_id)
            db.execute("DELETE FROM repository_progress WHERE repo_id=?", (repo_id,))
        return {"cleared": True}

    def repository_explain(self, repo_id, node_id, topic, level, scan_id, use_local_assistant=False):
        if not isinstance(node_id, str) or topic not in ("overview", "inputs", "returns", "structure", "calls", "connections") or level not in ("beginner", "explore", "technical"):
            raise ServiceError(422, "bad_explanation_request")
        if type(use_local_assistant) is not bool:
            raise ServiceError(422, "bad_explanation_request")
        with self.store.lock:
            self._check_repository_scan(repo_id, scan_id)
            if use_local_assistant and self.repository_content_access.get(repo_id) != "adult":
                raise ServiceError(403, "assistant_adult_only", "Local model explanations are only available in adult sessions")
            node = self.repository_snapshots.get(repo_id, {}).get(node_id)
            if not node:
                raise ServiceError(409, "stale_repository", "Select an item in the current scan")
            edges = self.repository_indexes.get(repo_id) or list(self.repository_edges.get(repo_id, []))
        from .repository_learning import explain_with_provider
        result = explain_with_provider(node, edges, topic, level)
        if use_local_assistant:
            result["assistant"] = {"status": "unavailable"}
            with self.store.lock:
                self._check_repository_scan(repo_id, scan_id)
                assistant = self.local_assistant
                approved = assistant is not None and self.repository_assistant_permitted.get(repo_id) is assistant
                root = self.repository_roots.get(repo_id, (None, None))[1]
            if approved and root and result["evidence_cards"]:
                try:
                    safe = assistant.safe_for(root) and assistant.available()
                    with self.store.lock:
                        self._check_repository_scan(repo_id, scan_id)
                        still_approved = (self.local_assistant is assistant and
                                          self.repository_assistant_permitted.get(repo_id) is assistant and
                                          self.repository_content_access.get(repo_id) == "adult")
                    if safe and still_approved:
                        answer = assistant.generate(result["evidence_cards"], topic, level)
                        result["assistant"] = {"status": "ready", **answer,
                            "limitation": "Model-generated interpretation; citations identify provided excerpts, not verified support for every claim. Check the source. No execution was observed."}
                except Exception:
                    result["assistant"] = {"status": "unavailable"}
            with self.store.lock:
                self._check_repository_scan(repo_id, scan_id)
                if self.local_assistant is not assistant:
                    result["assistant"] = {"status": "unavailable"}
        return result

    def repository_guided_path(self, repo_id, node_id, goal, level, prefer_unreviewed, scan_id):
        if (not isinstance(node_id, str) or goal not in ("understand", "calls", "dependencies") or
                level not in ("beginner", "explore", "technical") or type(prefer_unreviewed) is not bool):
            raise ServiceError(422, "bad_guided_path_request")
        with self.store.lock:
            self._check_repository_scan(repo_id, scan_id)
            nodes = self.repository_snapshots.get(repo_id, {})
            if node_id not in nodes:
                raise ServiceError(409, "stale_repository", "Select an item in the current scan")
            edges = self.repository_indexes.get(repo_id) or list(self.repository_edges.get(repo_id, []))
            # Review marks may change without a rescan; read the current local choices.
            marks = {r["node_id"]: r["fingerprint"] for r in self.store.q(
                "SELECT node_id,fingerprint FROM repository_progress WHERE repo_id=?", (repo_id,))}

        def reviewed(ident):
            """A current mark: saved for this item and still matching the scanned source."""
            if ident not in marks:
                return False
            digest = nodes[ident].get("sha256") or nodes[ident].get("fingerprint")
            return not (marks[ident] and digest and marks[ident] != digest)
        from .repository_learning import guided_path
        return guided_path(nodes, edges, node_id, goal, level, prefer_unreviewed, reviewed=reviewed)

    def repository_label_path(self, repo_id, node_id, target_id, level, include_inferred, include_organization, scan_id):
        return self._repository_path(repo_id, node_id, target_id, level, include_inferred, include_organization, scan_id, require_label=True)

    def repository_item_path(self, repo_id, node_id, target_id, level, include_inferred, include_organization, scan_id):
        return self._repository_path(repo_id, node_id, target_id, level, include_inferred, include_organization, scan_id, require_label=False)

    def _repository_path(self, repo_id, node_id, target_id, level, include_inferred, include_organization, scan_id, *, require_label):
        if (not all(isinstance(x, str) and 0 < len(x) <= 2048 for x in (node_id, target_id)) or
                level not in ("beginner", "explore", "technical") or
                type(include_inferred) is not bool or type(include_organization) is not bool):
            raise ServiceError(422, "bad_label_path_request" if require_label else "bad_item_path_request")
        with self.store.lock:
            self._check_repository_scan(repo_id, scan_id)
            nodes = self.repository_snapshots.get(repo_id, {})
            if node_id not in nodes or target_id not in nodes:
                raise ServiceError(409, "stale_repository", "Choose items in the current scan")
            if require_label:
                label = self.store.q1("SELECT label FROM repository_labels WHERE repo_id=? AND node_id=?", (repo_id, target_id))
                if not label:
                    raise ServiceError(409, "label_unavailable", "Save a label on this item before using it as a destination")
            edges = self.repository_indexes.get(repo_id) or list(self.repository_edges.get(repo_id, []))
        from .repository_learning import path_between_items
        return path_between_items(nodes, edges, node_id, target_id, level, include_inferred, include_organization, labeled=require_label)

    def repository_possible_callers(self, repo_id, node_id, level, max_depth, scan_id):
        if (not isinstance(node_id, str) or level not in ("beginner", "explore", "technical") or
                type(max_depth) is not int or not 1 <= max_depth <= 3):
            raise ServiceError(422, "bad_caller_exploration")
        with self.store.lock:
            self._check_repository_scan(repo_id, scan_id)
            nodes = self.repository_snapshots.get(repo_id, {})
            if node_id not in nodes or nodes[node_id].get("kind") != "function":
                raise ServiceError(409, "caller_unavailable", "Select a function in the current scan")
            index = self.repository_indexes.get(repo_id)
        from .repository_learning import possible_callers
        return possible_callers(nodes, node_id, level, max_depth, index=index)

    # Reading a file again is allowed only to show text whose bytes still match the scan.
    MAX_SOURCE_VIEW_BYTES, MAX_SOURCE_VIEW_LINES, MAX_SOURCE_VIEW_LINE_CHARS = 2_000_000, 20_000, 2000

    def _repository_file_text(self, repo_id, file_node):
        """(status, text). status is current, changed or unavailable; text is None unless current."""
        from .repository_map import _read_source, normalize_source
        root = self.repository_roots.get(repo_id, (None, None))[1]
        if not root or not file_node.get("sha256"):
            return "unavailable", None
        try:
            raw = _read_source(Path(root), Path(*file_node["path"].split("/")), self.MAX_SOURCE_VIEW_BYTES)
        except (OSError, ValueError):
            return "unavailable", None
        if len(raw) > self.MAX_SOURCE_VIEW_BYTES or hashlib.sha256(raw).hexdigest() != file_node["sha256"]:
            return "changed", None
        data, _ = normalize_source(raw)
        if data is None:
            return "unavailable", None
        return "current", data.decode("utf-8-sig")

    def repository_source(self, repo_id, node_id, scan_id):
        """The scanned file's full text, shown only while its bytes still match the scan."""
        if not isinstance(node_id, str):
            raise ServiceError(422, "bad_source_request")
        with self.store.lock:
            self._check_repository_scan(repo_id, scan_id)
            nodes = self.repository_snapshots.get(repo_id, {})
            node = nodes.get(node_id)
            file_node = nodes.get("file:" + str((node or {}).get("path") or ""))
            if not node or not file_node:
                raise ServiceError(409, "source_unavailable", "Select a file or an item inside a file")
        status, text = self._repository_file_text(repo_id, file_node)
        result = {"status": status, "path": file_node["path"], "sha256": file_node.get("sha256"), "lines": [], "truncated": False}
        if text is not None:
            lines = text.splitlines()
            result["line_count"] = len(lines)
            result["truncated"] = len(lines) > self.MAX_SOURCE_VIEW_LINES
            result["lines"] = [line[:self.MAX_SOURCE_VIEW_LINE_CHARS] for line in lines[:self.MAX_SOURCE_VIEW_LINES]]
            result["long_lines"] = any(len(line) > self.MAX_SOURCE_VIEW_LINE_CHARS for line in lines[:self.MAX_SOURCE_VIEW_LINES])
        with self.store.lock:
            self._check_repository_scan(repo_id, scan_id)
        return result

    def repository_search_source(self, repo_id, query, scan_id, limit=200, should_cancel=None, progress=None, seconds=120):
        """Find text in scanned files. Only files whose bytes still match the scan are searched."""
        import time as _time
        if not isinstance(query, str) or not 2 <= len(query.strip()) <= 200 or type(limit) is not int or not 1 <= limit <= 1000:
            raise ServiceError(422, "bad_search_request", "Enter between 2 and 200 characters to search for")
        needle = query.strip().casefold()
        with self.store.lock:
            self._check_repository_scan(repo_id, scan_id)
            nodes = self.repository_snapshots.get(repo_id, {})
            index = self.repository_indexes.get(repo_id)
            files = [n for n in nodes.values() if n["kind"] == "file"]
        matches, searched, changed, unavailable, stopped = [], 0, 0, 0, None
        deadline = _time.monotonic() + seconds
        for position, file_node in enumerate(files):
            if should_cancel is not None and should_cancel():
                stopped = "cancelled"; break
            if _time.monotonic() > deadline:
                stopped = "time limit"; break
            if len(matches) >= limit:
                stopped = "result limit"; break
            status, text = self._repository_file_text(repo_id, file_node)
            if status == "changed":
                changed += 1
            elif status != "current":
                unavailable += 1
            if text is None:
                continue
            searched += 1
            if needle in text.casefold():
                for number, line in enumerate(text.splitlines(), 1):
                    if needle in line.casefold():
                        owner = index.enclosing(file_node["path"], number) if index else file_node["id"]
                        matches.append({"path": file_node["path"], "line": number, "text": line.strip()[:300],
                                        "truncated": len(line.strip()) > 300, "node_id": owner or file_node["id"],
                                        "sha256": file_node["sha256"]})
                        if len(matches) >= limit:
                            break
            if progress is not None and position % 50 == 0:
                progress("Searching source", position, len(files), file_node["path"])
        with self.store.lock:
            self._check_repository_scan(repo_id, scan_id)
        return {"query": query.strip(), "matches": matches, "files_searched": searched, "files_total": len(files),
                "files_changed_since_scan": changed, "files_unavailable": unavailable, "stopped": stopped,
                "limitation": "Text search over scanned files whose bytes still match the scan; case is ignored. "
                              "A match is written text, not a relationship, and files that changed, were skipped or were not scanned are not searched."}

    def repository_compare_call(self, repo_id, node_id, call_index, level, scan_id):
        if not isinstance(node_id, str) or type(call_index) is not int or call_index < 0 or level not in ("beginner", "explore", "technical"):
            raise ServiceError(422, "bad_call_comparison")
        with self.store.lock:
            self._check_repository_scan(repo_id, scan_id)
            nodes = self.repository_snapshots.get(repo_id, {})
            node = nodes.get(node_id)
            calls = node.get("call_sites", []) if node else []
            if call_index >= len(calls):
                raise ServiceError(409, "call_unavailable", "Select a written call from the current scan")
            call = calls[call_index]
            target = nodes.get(call.get("target_id"))
            if call.get("resolution") != "inferred" or not target or target.get("kind") != "function":
                raise ServiceError(409, "target_unresolved", "No possible target is available for this call")
            call, target = dict(call), dict(target)
        from .repository_learning import compare_call_with_target
        return compare_call_with_target(call, target, level)

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
                      dialect: str | None = None, replaces: str | None = None, *, retry: bool = False) -> dict:
        if dialect is not None and not isinstance(dialect, str):
            raise ServiceError(422, "bad_dialect", "dialect must be a string")
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
            existing = None if retry else db.execute(
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
        # An explicit retry is not a duplicate upload: it must preserve a distinct,
        # auditable attempt even when the previous extraction was successful.
        if dialect is None and src["detected_format"] == "sql":
            latest = self.store.q1("SELECT dialect FROM attempts WHERE source_id=? ORDER BY created_seq DESC LIMIT 1",
                                   (source_id,))
            dialect = latest["dialect"] if latest else None
        return self.import_source(data, src["display_name"], src["detected_format"], dialect, retry=True)

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
        latest = attempts[-1] if attempts else None
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
                "latest_attempt": None if not latest else {
                    "id": latest["id"], "state": latest["state"], "coverage": latest["summary"],
                    "dialect": latest["dialect"], "error": jload(latest["error_json"])},
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
        if not isinstance(action, str):
            raise ServiceError(422, "bad_action", "action must be a string")
        if action not in ACTIONS:
            raise ServiceError(422, "bad_action", action)
        if reason is not None and not isinstance(reason, str):
            raise ServiceError(422, "bad_reason", "reason must be a string")
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
                 "coherence": p.body.get("coherence", []), "lifecycle": p.body.get("lifecycle"),
                 "applies_when": p.body.get("applies_when"),
                 "applicability_facts": sorted(set(_pred_facts(p.body.get("applies_when"))))} for p in self.purposes.list()]

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
        if packs or any("applies_when" in prof.body for prof, _ in purpose_reqs):
            add_blob(canonical_bytes(facts or {}), CONTEXT_MEDIA)       # pack applicability and purpose applicability read the same pinned facts
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
            result["purposes"] = [PU.evaluate_purpose(pr, b, ev_sources, facts or {}) for pr, b in purpose_reqs]
            if not packs and any("applies_when" in pr.body for pr, _ in purpose_reqs):
                result["context_digest"] = RP.context_digest(facts or {})
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

    def workspace_graph(self, limit: int = 200) -> dict:
        """What depends on what: sources -> claim revisions -> runs, with replacement links and run staleness.

        Edges point from a dependency to what depends on it, so everything downstream of a node is what its change can invalidate."""
        nodes: dict[str, dict] = {}
        edges: set[tuple[str, str, str]] = set()
        truncated = False
        sources = self.store.q("SELECT id, display_name, superseded_by FROM sources ORDER BY created_seq DESC LIMIT ?", (limit + 1,))
        claims = self.store.q("SELECT id, candidate_id, status, body_json FROM claim_revisions ORDER BY created_seq DESC LIMIT ?", (limit + 1,))
        runs = self.store.q("SELECT * FROM runs ORDER BY created_seq DESC LIMIT ?", (limit + 1,))
        for rows in (sources, claims, runs):
            truncated = truncated or len(rows) > limit
        for r in sources[:limit]:
            nodes[r["id"]] = {"id": r["id"], "kind": "source", "label": r["display_name"],
                              "state": "SUPERSEDED" if r["superseded_by"] else "CURRENT"}
            if r["superseded_by"]:
                edges.add((r["id"], r["superseded_by"], "replaced_by"))
        for r in claims[:limit]:
            body = jload(r["body_json"])
            nodes[r["id"]] = {"id": r["id"], "kind": "claim", "label": f'{body["subject"]} ({body["predicate"]})', "state": r["status"]}
            edges.add((body["origin"]["source_id"], r["id"], "states"))
        for r in runs[:limit]:
            deps = jload(r["deps_json"])
            fresh = self._freshness(r)
            nodes[r["id"]] = {"id": r["id"], "kind": "run", "label": "run " + r["id"][-8:], "state": r["state"] if fresh["state"] == "UNKNOWN" else fresh["state"],
                              "causes": [c["kind"] for c in fresh["causes"]]}
            for rid in deps["claim_revisions"]:
                edges.add((rid, r["id"], "tested"))
            for sid in deps["evidence"]:
                edges.add((sid, r["id"], "evidence"))
        edges = {e for e in edges if e[0] in nodes and e[1] in nodes}           # a truncated view never shows dangling edges
        layer = {"source": 0, "claim": 1, "run": 2}
        return {"nodes": sorted(nodes.values(), key=lambda n: (layer[n["kind"]], n["label"], n["id"])),
                "edges": [{"from": a, "to": b, "relation": rel} for a, b, rel in sorted(edges)],
                "truncated": truncated, "limit": limit,
                "note": "Downstream of a node is what a change to it can invalidate. Staleness is the stored run freshness check, not a re-evaluation."}

    def compare_runs(self, new_run_id: str, old_run_id: str) -> dict:
        """Which targeted composition results an evidence change cannot have altered: equal evaluation keys, same claim."""
        new, old = self.get_run(new_run_id), self.get_run(old_run_id)
        for run in (new, old):
            if run["state"] != "SUCCEEDED":
                raise ServiceError(409, "run_not_complete", run["id"])

        def keyed(run):
            out = {}
            for f in run["findings"]:
                analysis = (f.get("witness") or {}).get("analysis") or {}
                if analysis.get("evaluation_key") and f.get("claim"):
                    out[f["claim"]["claim_revision_id"]] = f
            return out
        before, after = keyed(old), keyed(new)
        rows = []
        for revision_id, f in sorted(after.items()):
            key = f["witness"]["analysis"]["evaluation_key"]
            prev = before.get(revision_id)
            if prev is None:
                status = "NOT_IN_EARLIER_RUN"
            elif prev["witness"]["analysis"]["evaluation_key"] != key or prev["rule_version"] != f["rule_version"]:
                status = "MUST_REEVALUATE"
            else:
                status = "UNCHANGED"
            rows.append({"claim_revision_id": revision_id, "subject": f["claim"]["subject"],
                         "target": f["witness"]["analysis"]["scope"]["target"], "status": status,
                         "verdict": f["verdict"], "earlier_verdict": prev["verdict"] if prev else None,
                         "evaluation_key": key})
        return {"run": new_run_id, "against": old_run_id, "items": rows,
                "statement": "UNCHANGED means the target's ancestors, the declarations it reads and the rule version are identical, so its "
                             "obligations cannot differ; it does not mean the evidence file is byte-identical. Results without an "
                             "evaluation key (untargeted checks, other predicates) are not listed and must be re-evaluated."}

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
        defects = _name_list(defects, "defects")
        bad = [d for d in defects if d not in DEFECTS]
        if bad:
            raise ServiceError(422, "unknown_defect", f"{bad}; known: {list(DEFECTS)}")
        if boundary is not None and (not isinstance(boundary, int) or isinstance(boundary, bool) or not 0 <= boundary <= ops):
            raise ServiceError(422, "bad_boundary", "boundary must be an integer between 0 and ops")
        b = ops // 2 if boundary is None else boundary
        return ExperimentCase(seed, tuple(generate_history(seed, ops)), b, tuple(defects or []))

    def experiment_checkpoint(self, seed="seed-1", ops=60, boundary=None, defects=None) -> dict:
        from .experiments.checkpoint import run_experiment
        case = self._case(seed, ops, boundary, defects)
        return {"case": case.identity(), "defects": list(case.defects), **run_experiment(case).to_dict()}

    def experiment_crash(self, writer_defect=None, recovery_defects=None, max_positions=None) -> dict:
        from .experiments import crash
        recovery_defects = _name_list(recovery_defects, "recovery_defects")
        if writer_defect not in (None, *crash.WRITER_DEFECTS) or any(d not in crash.RECOVERY_DEFECTS for d in recovery_defects):
            raise ServiceError(422, "unknown_defect")
        if max_positions is not None and (not isinstance(max_positions, int) or isinstance(max_positions, bool) or max_positions < 0):
            raise ServiceError(422, "bad_max_positions", "max_positions must be a non-negative integer")
        return crash.crash_experiment(writer_defect, tuple(recovery_defects), max_positions).to_dict()

    def experiment_minimize(self, seed="seed-1", ops=120, boundary=None, defects=None, max_oracle_calls=3000) -> dict:
        import tempfile
        from .experiments.package import CheckpointOracle, build_package, verify_package_fresh_process, write_package
        from .minimize import ReductionBudget, minimize
        from .reducers.sequence import DeleteTransactionMembers, DeleteUnits
        from .reducers.values import SimplifyValues
        if not isinstance(max_oracle_calls, int) or isinstance(max_oracle_calls, bool) or max_oracle_calls < 1:
            raise ServiceError(422, "bad_budget", "max_oracle_calls must be a positive integer")
        if not defects:
            raise ServiceError(422, "defect_required", "inject at least one named defect to have a failure to reduce")
        case = self._case(seed, ops, boundary, defects)
        reducers = [DeleteUnits(), DeleteTransactionMembers(), SimplifyValues()]
        result = minimize(case, CheckpointOracle(), reducers, ReductionBudget(max_oracle_calls=min(max_oracle_calls, 3000)))
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
                "parser_isolation": {"mode": self.limits.isolation,
                    "backend": "linux-bubblewrap+seccomp" if self.limits.isolation == "required" else "portable-resource-limits",
                    "note": "required mode fails closed when the host cannot establish isolation"},
                "limits": self.limits.to_dict(), "max_upload_bytes": self.admission.max_bytes}


def _pred_facts(p) -> list[str]:
    if not p:
        return []
    if p["op"] in ("eq", "in", "exists"):
        return [p["fact"]]
    return [f for a in (p.get("args") or [p["arg"]] if "arg" in p else p["args"]) for f in _pred_facts(a)]


def _name_list(value, field: str) -> list:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)) or not all(isinstance(v, str) for v in value):
        raise ServiceError(422, "bad_" + field, f"{field} must be a list of names")
    return list(value)

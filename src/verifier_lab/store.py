"""SQLite workspace storage. Application state only; evidence bytes live in content-addressed blobs."""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources(
  id TEXT PRIMARY KEY, digest TEXT UNIQUE NOT NULL, size INTEGER NOT NULL, display_name TEXT NOT NULL,
  detected_format TEXT NOT NULL, created_seq INTEGER NOT NULL, created_at TEXT NOT NULL,
  current_attempt TEXT, superseded_by TEXT, replaces TEXT);
CREATE TABLE IF NOT EXISTS attempts(
  id TEXT PRIMARY KEY, source_id TEXT NOT NULL, manifest_id TEXT, dialect TEXT, state TEXT NOT NULL,
  summary TEXT, record_digest TEXT, rendition_digest TEXT, error_json TEXT,
  created_seq INTEGER NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS candidates(
  id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL, source_id TEXT NOT NULL, fact_key TEXT NOT NULL,
  predicate_json TEXT NOT NULL, span_id TEXT NOT NULL, qualifiers_json TEXT NOT NULL, ord INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS cand_attempt ON candidates(attempt_id, ord);
CREATE TABLE IF NOT EXISTS authored_spans(
  candidate_id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL, span_id TEXT NOT NULL,
  fact_json TEXT NOT NULL, reason TEXT NOT NULL, UNIQUE(attempt_id, span_id, candidate_id));
CREATE TABLE IF NOT EXISTS decisions(
  id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL, n INTEGER NOT NULL, action TEXT NOT NULL,
  reason TEXT, body_json TEXT NOT NULL, claim_revision_id TEXT, created_seq INTEGER NOT NULL,
  created_at TEXT NOT NULL, UNIQUE(candidate_id, n));
CREATE TABLE IF NOT EXISTS claim_revisions(
  id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL, rev INTEGER NOT NULL, decision_id TEXT NOT NULL,
  body_json TEXT NOT NULL, status TEXT NOT NULL, created_seq INTEGER NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS jobs(
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, state TEXT NOT NULL, stage TEXT, done INTEGER, total INTEGER,
  payload_json TEXT, result_json TEXT, error_json TEXT, cancel_requested INTEGER NOT NULL DEFAULT 0,
  created_seq INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, heartbeat TEXT);
CREATE TABLE IF NOT EXISTS job_events(
  job_id TEXT NOT NULL, seq INTEGER NOT NULL, type TEXT NOT NULL, body_json TEXT NOT NULL,
  PRIMARY KEY(job_id, seq));
CREATE TABLE IF NOT EXISTS runs(
  id TEXT PRIMARY KEY, job_id TEXT NOT NULL, state TEXT NOT NULL, input_json TEXT NOT NULL,
  deps_json TEXT NOT NULL, summary_json TEXT, package_dir TEXT, bundle_id TEXT,
  created_seq INTEGER NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS idempotency(
  scope TEXT NOT NULL, key TEXT NOT NULL, payload_hash TEXT NOT NULL, response_json TEXT NOT NULL,
  PRIMARY KEY(scope, key));
CREATE TABLE IF NOT EXISTS seq(name TEXT PRIMARY KEY, value INTEGER NOT NULL);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "blobs").mkdir(exist_ok=True)
        (self.root / "runs").mkdir(exist_ok=True)
        (self.root / "exports").mkdir(exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.root / "verifier.db", check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(SCHEMA)

    @property
    def blobs(self) -> Path:
        return self.root / "blobs"

    def next_seq(self, name: str = "global") -> int:
        with self.lock:
            self.db.execute("INSERT INTO seq(name,value) VALUES(?,1) ON CONFLICT(name) DO UPDATE SET value=value+1", (name,))
            return self.db.execute("SELECT value FROM seq WHERE name=?", (name,)).fetchone()[0]

    def tx(self):
        return _Tx(self)

    def q(self, sql: str, args=()):
        with self.lock:
            return self.db.execute(sql, args).fetchall()

    def q1(self, sql: str, args=()):
        with self.lock:
            return self.db.execute(sql, args).fetchone()

    def x(self, sql: str, args=()):
        with self.lock:
            return self.db.execute(sql, args)

    def close(self):
        with self.lock:
            self.db.close()


class _Tx:
    def __init__(self, store: Store):
        self.store = store

    def __enter__(self):
        self.store.lock.acquire()
        self.store.db.execute("BEGIN IMMEDIATE")
        return self.store.db

    def __exit__(self, exc_type, exc, tb):
        try:
            self.store.db.execute("ROLLBACK" if exc_type else "COMMIT")
        finally:
            self.store.lock.release()
        return False


def jdump(v) -> str:
    return json.dumps(v, ensure_ascii=False, sort_keys=True)


def jload(s):
    return None if s is None else json.loads(s)

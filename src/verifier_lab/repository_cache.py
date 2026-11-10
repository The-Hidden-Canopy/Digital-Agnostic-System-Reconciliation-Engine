"""Local, disposable cache of per-file syntax records for incremental repository rescans.

A record is reused only when the file path, its exact bytes (SHA-256), the language and the
parser revision all match, so a rescan re-parses exactly the files that changed. The cache
holds bounded source excerpts from scanned repositories; it lives in the local workspace and
can be cleared at any time without losing labels or review marks.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
import zlib
from pathlib import Path

# Bump whenever a parser or the per-file record layout changes.
PARSER_REVISION = "elucidary-parse-5"
MAX_CACHE_BYTES = 1_500_000_000


def record_key(path: str, digest: str, language: str, shape: str) -> str:
    return hashlib.sha256("\0".join((PARSER_REVISION, language, path, digest, shape)).encode("utf-8")).hexdigest()


class ParseCache:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.hits = self.misses = self.pending = 0
        self.db = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        try:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=OFF")
            self.db.execute("CREATE TABLE IF NOT EXISTS parsed(key TEXT PRIMARY KEY, payload BLOB NOT NULL, "
                            "size INTEGER NOT NULL, used REAL NOT NULL)")
            self.db.commit()
        except sqlite3.DatabaseError:
            self._reset()

    def _reset(self):
        """A damaged cache is discarded, never trusted."""
        try:
            self.db.close()
        except sqlite3.Error:
            pass
        for suffix in ("", "-wal", "-shm"):
            try:
                Path(str(self.path) + suffix).unlink()
            except OSError:
                pass
        self.db = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        self.db.execute("CREATE TABLE IF NOT EXISTS parsed(key TEXT PRIMARY KEY, payload BLOB NOT NULL, "
                        "size INTEGER NOT NULL, used REAL NOT NULL)")
        self.db.commit()

    def get(self, key: str):
        with self.lock:
            try:
                row = self.db.execute("SELECT payload FROM parsed WHERE key=?", (key,)).fetchone()
            except sqlite3.DatabaseError:
                row = None
        if row is None:
            self.misses += 1
            return None
        try:
            record = json.loads(zlib.decompress(row[0]))
        except (zlib.error, ValueError):
            self.misses += 1
            return None
        self.hits += 1
        return record

    def put(self, key: str, record: dict):
        payload = zlib.compress(json.dumps(record, separators=(",", ":")).encode("utf-8"), 1)
        with self.lock:
            try:
                self.db.execute("INSERT OR REPLACE INTO parsed(key,payload,size,used) VALUES(?,?,?,?)",
                                (key, payload, len(payload), time.time()))
                self.pending += 1
                if self.pending >= 400:
                    self.db.commit()
                    self.pending = 0
            except sqlite3.DatabaseError:
                pass

    def touch(self, keys):
        """Mark records used by the scan that just finished, then drop the oldest beyond the size bound."""
        now = time.time()
        with self.lock:
            try:
                self.db.executemany("UPDATE parsed SET used=? WHERE key=?", ((now, key) for key in keys))
                total = self.db.execute("SELECT COALESCE(SUM(size),0) FROM parsed").fetchone()[0]
                if total > MAX_CACHE_BYTES:
                    excess = total - int(MAX_CACHE_BYTES * 0.8)
                    for key, size in self.db.execute("SELECT key,size FROM parsed ORDER BY used").fetchall():
                        if excess <= 0:
                            break
                        self.db.execute("DELETE FROM parsed WHERE key=?", (key,))
                        excess -= size
                self.db.commit()
                self.pending = 0
            except sqlite3.DatabaseError:
                pass

    def stats(self):
        with self.lock:
            try:
                count, size = self.db.execute("SELECT COUNT(*), COALESCE(SUM(size),0) FROM parsed").fetchone()
            except sqlite3.DatabaseError:
                count, size = 0, 0
        return {"records": count, "bytes": size}

    def clear(self):
        with self.lock:
            try:
                self.db.execute("DELETE FROM parsed")
                self.db.commit()
                self.db.execute("VACUUM")
            except sqlite3.DatabaseError:
                pass
            self.pending = 0

    def close(self):
        with self.lock:
            try:
                self.db.commit()
                self.db.close()
            except sqlite3.Error:
                pass

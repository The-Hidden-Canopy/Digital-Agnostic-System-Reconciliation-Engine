"""Run an adapter in an isolated subprocess and turn its output into an extraction record."""
from __future__ import annotations

import base64
import json
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from ..canonical import record_id, sha256_digest
from .common import Limits, extraction_manifest, summarize
from .worker import run_in_process, to_wire
from .isolation import IsolationUnavailable, command

PKG_ROOT = str(Path(__file__).resolve().parents[2])
BOOT = ("import sys; sys.path.insert(0, sys.argv[1]); "
        "from verifier_lab.ingestion.worker import main; sys.exit(main(sys.argv[1:]))")
MAX_OUTPUT = 64_000_000


class ExtractionFailure(Exception):
    def __init__(self, code: str, message: str = ""):
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code


def extract_isolated(fmt: str, data: bytes, dialect: str | None, limits: Limits,
                     should_cancel=lambda: False) -> dict:
    request = json.dumps({"format": fmt, "dialect": dialect, "limits": limits.to_dict()})
    try:
        argv = command([sys.executable, "-E", "-s", "-B", "-c", BOOT, PKG_ROOT, request], PKG_ROOT, limits.isolation)
    except IsolationUnavailable as exc:
        raise ExtractionFailure("isolation_unavailable", str(exc)) from exc
    deadline = time.monotonic() + limits.wall_seconds
    with tempfile.TemporaryDirectory(prefix="vl-worker-") as cwd:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd,
            env={"PATH": "", "LANG": "C.UTF-8"})
        chunks, exceeded = {}, threading.Event()

        def read_limited(name, stream, bound):
            try:
                value = stream.read(bound + 1)
                chunks[name] = value[:bound]
                if len(value) > bound:
                    exceeded.set()
                    proc.kill()
            finally:
                stream.close()

        def send_input():
            try:
                proc.stdin.write(data)
            except (BrokenPipeError, OSError):
                pass
            finally:
                try:
                    proc.stdin.close()
                except OSError:
                    pass

        threads = [threading.Thread(target=read_limited, args=("stdout", proc.stdout, MAX_OUTPUT)),
                   threading.Thread(target=read_limited, args=("stderr", proc.stderr, 65536)),
                   threading.Thread(target=send_input)]
        for thread in threads:
            thread.start()
        try:
            while True:
                if should_cancel():
                    raise ExtractionFailure("cancelled")
                if time.monotonic() > deadline:
                    raise ExtractionFailure("wall_clock_timeout")
                try:
                    proc.wait(timeout=0.05)
                    break
                except subprocess.TimeoutExpired:
                    continue
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait()
            for thread in threads:
                thread.join()
        stdout = chunks.get("stdout", b"")
    if exceeded.is_set():
        raise ExtractionFailure("output_limit")
    if proc.returncode != 0:
        if limits.isolation == "required" and chunks.get("stderr", b"").startswith(b"bwrap:"):
            raise ExtractionFailure("isolation_unavailable", "the host cannot start the Linux parser sandbox")
        raise ExtractionFailure("worker_crashed", f"exit {proc.returncode}")
    try:
        out = json.loads(stdout)
    except ValueError as exc:
        raise ExtractionFailure("worker_protocol_error") from exc
    if not out.get("ok"):
        raise ExtractionFailure(out.get("error", "worker_error"))
    return out["extraction"]


def build_record(source_digest: str, source_bytes: bytes, wire: dict, limits: Limits,
                 options: dict) -> tuple[dict, bytes]:
    """Assemble the extraction record artifact (facts + coverage + manifest) and rendition bytes."""
    rendition = source_bytes if wire["rendition_b64"] is None else base64.b64decode(wire["rendition_b64"])
    rend_digest = sha256_digest(rendition)

    class _E:  # minimal shim for manifest creation
        format, parser, parser_version, dialect = (wire["format"], wire["parser"],
                                                   wire["parser_version"], wire["dialect"])
    manifest = extraction_manifest(source_digest, _E, limits, options)
    facts = []
    for f in wire["facts"]:
        span_body = {"source_digest": source_digest, "rendition_digest": rend_digest,
                     "extraction_manifest_id": manifest["id"], "locator": f["locator"]}
        span_id = record_id("span", span_body)
        facts.append({**f, "span_id": span_id})
    status = summarize(wire["regions"])
    record = {"schema": "verifier-lab/extraction-record/1", "manifest": manifest,
              "source_digest": source_digest, "rendition_digest": rend_digest,
              "rendition_is_source": wire["rendition_b64"] is None,
              "coverage": {"summary": status, "regions": wire["regions"]},
              "facts": facts, "blocked_keys": wire["blocked_keys"],
              "diagnostics": wire["diagnostics"]}
    return record, rendition


def extract_local(fmt: str, data: bytes, dialect: str | None, limits: Limits) -> dict:
    """In-process variant used only by tests that exercise adapters directly."""
    return to_wire(run_in_process(fmt, data, dialect, limits))

"""Loopback-only HTTP API + static workbench. Local does not mean trusted (chapter 16)."""
from __future__ import annotations

import json
import secrets
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .service import Service, ServiceError

LOOPBACK = ("127.0.0.1", "::1", "localhost")
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
       "img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
STATIC = {"/static/app.js": ("app.js", "text/javascript; charset=utf-8"),
          "/static/style.css": ("style.css", "text/css; charset=utf-8")}


def _web(name: str) -> bytes:
    return resources.files("verifier_lab").joinpath("web", name).read_bytes()


class Workbench(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, service: Service, host: str = "127.0.0.1", port: int = 0):
        if host not in LOOPBACK:
            raise ValueError("the workbench binds to loopback only; remote deployment is a separate release gate")
        self.service = service
        self.token = secrets.token_urlsafe(32)
        super().__init__((host, port), Handler)
        self.allowed_hosts = {f"127.0.0.1:{self.server_port}", f"localhost:{self.server_port}",
                              f"[::1]:{self.server_port}"}

    def handle_error(self, request, client_address):
        import sys
        if isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError)):
            return  # client went away mid-response; nothing to report
        super().handle_error(request, client_address)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_port}/"


class Handler(BaseHTTPRequestHandler):
    server: Workbench
    protocol_version = "HTTP/1.1"
    server_version = "verifier-lab"

    def log_message(self, fmt, *args):  # quiet; diagnostics go to stderr only on errors
        pass

    # ---- plumbing
    def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", CSP)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, obj, extra: dict | None = None):
        self._send(status, json.dumps(obj).encode(), "application/json; charset=utf-8", extra)

    def _guard(self, mutating: bool) -> bool:
        if self.headers.get("Host", "") not in self.server.allowed_hosts:
            self._json(421, {"error": {"code": "bad_host"}})
            return False
        origin = self.headers.get("Origin")
        if origin is not None and origin not in {f"http://{h}" for h in self.server.allowed_hosts}:
            self._json(403, {"error": {"code": "bad_origin"}})
            return False
        if self.path.startswith("/api/"):
            if not secrets.compare_digest(self.headers.get("X-Session-Token", ""), self.server.token):
                self._json(401, {"error": {"code": "session_token_required"}})
                return False
        return True

    def _body(self, limit: int) -> bytes:
        if self._raw is not None:
            return self._raw
        length = self.headers.get("Content-Length")
        if length is None or not length.isdigit():
            raise ServiceError(411, "length_required")
        n = int(length)
        if n > limit:
            raise ServiceError(413, "size_limit", f"{n} > {limit}")
        data = self.rfile.read(n)
        if len(data) != n:
            raise ServiceError(400, "truncated_body")
        self._raw = data
        return data

    def _json_body(self) -> dict:
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            raise ServiceError(415, "json_required")
        try:
            v = json.loads(self._body(1_000_000) or b"{}")
        except ValueError:
            raise ServiceError(400, "malformed_json")
        if not isinstance(v, dict):
            raise ServiceError(400, "object_required")
        return v

    # ---- dispatch
    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_HEAD(self):
        self._dispatch("GET")

    _raw: bytes | None = None

    def _dispatch(self, method: str):
        self._raw = None
        try:
            if not self._guard(method == "POST"):
                return
            if method == "POST":  # always drain the body so keep-alive framing stays intact
                self._body(self.server.service.admission.max_bytes + 1)
            url = urlparse(self.path)
            path, qs = url.path, {k: v[0] for k, v in parse_qs(url.query, keep_blank_values=True).items()}
            if path == "/" and method == "GET":
                html = _web("index.html").replace(b"__TOKEN__", self.server.token.encode())
                return self._send(200, html, "text/html; charset=utf-8")
            if path == "/favicon.ico":
                return self._send(204, b"", "image/x-icon")
            if path in STATIC and method == "GET":
                name, ctype = STATIC[path]
                return self._send(200, _web(name), ctype)
            if path.startswith("/api/v1/"):
                return self._api(method, path[len("/api/v1/"):], qs)
            self._json(404, {"error": {"code": "not_found"}})
        except ServiceError as exc:
            extra = {"ETag": exc.details["etag"]} if "etag" in exc.details else None
            self._json(exc.status, exc.to_dict(), extra)
        except BrokenPipeError:
            pass
        except Exception as exc:  # never leak internals or tracebacks
            self._json(500, {"error": {"code": "internal_error", "message": type(exc).__name__}})

    def _api(self, method: str, route: str, qs: dict):
        svc = self.server.service
        parts = [unquote(p) for p in route.split("/")]
        h = self.headers
        if method == "GET":
            if parts == ["overview"]:
                return self._json(200, svc.overview())
            if parts == ["capabilities"]:
                return self._json(200, svc.capabilities())
            if parts == ["sources"]:
                return self._json(200, {"items": svc.list_sources()})
            if len(parts) == 2 and parts[0] == "sources":
                return self._json(200, svc.get_source(parts[1], detail=True))
            if len(parts) == 3 and parts[0] == "sources" and parts[2] == "text":
                return self._json(200, svc.source_text(parts[1]))
            if parts == ["candidates"]:
                return self._json(200, svc.list_candidates(
                    source_id=qs.get("source_id"), status=qs.get("status"), query=qs.get("q"),
                    limit=min(_int_param(qs, "limit", 100, 1), 500), offset=_int_param(qs, "offset", 0, 0)))
            if len(parts) == 2 and parts[0] == "candidates":
                cand = svc.get_candidate(parts[1])
                return self._json(200, cand, {"ETag": cand["etag"]})
            if parts == ["packs"]:
                return self._json(200, {"items": svc.list_packs(), "skipped": svc.packs.skipped})
            if parts == ["purposes"]:
                return self._json(200, {"items": svc.list_purposes()})
            if len(parts) == 2 and parts[0] == "composition":
                return self._json(200, svc.composition_analyze(parts[1], qs.get("subject", "pipeline"), qs.get("target")))
            if parts == ["claims"]:
                return self._json(200, {"items": svc.list_claims()})
            if parts == ["reconciliation"]:
                return self._json(200, {"items": svc.reconciliation()})
            if parts == ["runs"]:
                return self._json(200, {"items": svc.list_runs()})
            if len(parts) == 2 and parts[0] == "runs":
                return self._json(200, svc.get_run(parts[1]))
            if len(parts) == 3 and parts[0] == "runs" and parts[2] == "reuse":
                if not qs.get("against"):
                    raise ServiceError(422, "bad_query", "against (an earlier run id) is required")
                return self._json(200, svc.compare_runs(parts[1], qs["against"]))
            if len(parts) == 3 and parts[0] == "runs" and parts[2] == "export":
                path = svc.export_run(parts[1])
                return self._send(200, path.read_bytes(), "application/zip",
                                  {"Content-Disposition": 'attachment; filename="verification-package.zip"'})
            if parts == ["jobs"]:
                return self._json(200, {"items": svc.list_jobs()})
            if len(parts) == 2 and parts[0] == "jobs":
                return self._json(200, svc.get_job(parts[1]))
            if len(parts) == 3 and parts[0] == "jobs" and parts[2] == "events":
                return self._json(200, svc.job_events(parts[1], _int_param(qs, "after", 0, 0)))
        else:
            if parts == ["candidates"]:
                body = self._json_body()
                fields = {"source_id", "attempt_id", "byte_start", "byte_end", "interpretation", "reason"}
                if not isinstance(body, dict) or set(body) != fields:
                    raise ServiceError(422, "bad_author_request", "supply source, attempt, byte span, interpretation and reason")
                res = svc.author_candidate(**body, idempotency_key=h.get("Idempotency-Key"))
                return self._json(201, res, {"ETag": res["etag"]})
            if parts == ["sources"]:
                data = self._body(svc.admission.max_bytes + 1)
                res = svc.import_source(
                    data, unquote(h.get("X-Filename", "")), h.get("X-Declared-Type") or None,
                    h.get("X-Dialect") or None, h.get("X-Replaces") or None)
                return self._json(202 if res["job_id"] else 200, res)
            if len(parts) == 3 and parts[0] == "sources" and parts[2] == "reextract":
                body = self._json_body()
                return self._json(202, svc.reextract(parts[1], body.get("dialect")))
            if len(parts) == 3 and parts[0] == "candidates" and parts[2] == "decisions":
                body = self._json_body()
                res = svc.decide(parts[1], body.get("action", ""), h.get("If-Match"),
                                 edit=body.get("edit"), reason=body.get("reason"),
                                 idempotency_key=h.get("Idempotency-Key"))
                return self._json(200, res, {"ETag": res["etag"]})
            if len(parts) == 2 and parts[0] == "experiments":
                body = self._json_body()
                if any(k in body for k in ("subject_cmd", "kernel_cmd", "command", "subject", "executable")):
                    raise ServiceError(422, "commands_not_accepted_over_http",
                                       "running your own program is configured on the command line (--subject-cmd), never over the web API")
                if parts[1] == "checkpoint":
                    return self._json(200, svc.experiment_checkpoint(body.get("seed", "seed-1"), body.get("ops", 60), body.get("boundary"), body.get("defects")))
                if parts[1] in ("pipeline", "scheduler"):
                    return self._json(200, svc.experiment_laboratory(parts[1], body.get("seed", "seed-1"),
                        body.get("ops", 60), body.get("boundary"), body.get("defects")))
                if parts[1] == "laboratory-minimize":
                    return self._json(200, svc.experiment_laboratory(body.get("system"), body.get("seed", "seed-1"),
                        body.get("ops", 60), body.get("boundary"), body.get("defects"), True,
                        body.get("max_oracle_calls", 3000)))
                if parts[1] == "campaign":
                    return self._json(200, svc.campaign_run(body.get("kernel", "real"), body.get("seed", "campaign-1")))
                if parts[1] == "campaign-verify":
                    return self._json(200, svc.campaign_verify(body.get("report"), body.get("manifest"), body.get("trusted_manifest_digest")))
                if parts[1] == "crash":
                    return self._json(200, svc.experiment_crash(body.get("writer_defect"), body.get("recovery_defects"), body.get("max_positions")))
                if parts[1] == "minimize":
                    return self._json(200, svc.experiment_minimize(body.get("seed", "seed-1"), body.get("ops", 120), body.get("boundary"), body.get("defects"), body.get("max_oracle_calls", 3000)))
            if parts == ["composition", "impact"]:
                body = self._json_body()
                return self._json(200, svc.composition_impact(body.get("old", ""), body.get("new", ""), body.get("subject", "pipeline")))
            if parts == ["purposes"]:
                self._json_body()
                return self._json(201, svc.install_purpose(self._raw))
            if parts == ["packs"]:
                self._json_body()  # content-type/shape check; the raw bytes are what gets digest-verified
                return self._json(201, svc.install_pack(self._raw))
            if parts == ["runs"]:
                body = self._json_body()
                lists = {k: _string_list(body, k) for k in
                         ("claim_revision_ids", "evidence_source_ids", "pack_ids")}
                purposes = body["purposes"] if "purposes" in body else []
                if not isinstance(purposes, list) or not all(isinstance(p, dict) for p in purposes):
                    raise ServiceError(422, "bad_run_request", "purposes must be a list of objects")
                context = body["context"] if "context" in body else {}
                if not isinstance(context, dict):
                    raise ServiceError(422, "bad_run_request", "context must be an object")
                res = svc.start_run(lists["claim_revision_ids"], lists["evidence_source_ids"],
                                    h.get("Idempotency-Key"), lists["pack_ids"], context, purposes)
                return self._json(202, res)
            if len(parts) == 3 and parts[0] == "runs" and parts[2] == "verify":
                path = svc.export_run(parts[1])
                return self._json(200, svc.verify_export(path))
            if len(parts) == 3 and parts[0] == "jobs" and parts[2] == "cancel":
                return self._json(200, svc.cancel_job(parts[1]))
        self._json(404, {"error": {"code": "not_found"}})


def _int_param(qs: dict, name: str, default: int, minimum: int) -> int:
    if name not in qs:
        return default
    raw = qs[name]
    if not (raw.isascii() and raw.isdigit()) or int(raw) < minimum:
        raise ServiceError(422, "bad_query", f"{name} must be an integer >= {minimum}")
    return int(raw)


def _string_list(body: dict, name: str) -> list[str]:
    value = body[name] if name in body else []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ServiceError(422, "bad_run_request", f"{name} must be a list of strings")
    return value


def serve(workspace: Path, host: str = "127.0.0.1", port: int = 8765) -> Workbench:
    return Workbench(Service(workspace), host, port)

import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import quote

from tests.helpers import TUTORIAL
from verifier_lab.server import Workbench
from verifier_lab.service import Service


class ServerCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.svc = Service(Path(cls.tmp.name))
        cls.wb = Workbench(cls.svc, "127.0.0.1", 0)
        cls.port = cls.wb.server_port
        cls.thread = threading.Thread(target=cls.wb.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.wb.shutdown()
        cls.wb.server_close()
        cls.svc.close()
        cls.tmp.cleanup()

    def conn(self):
        return http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)

    def call(self, method, path, body=None, headers=None, conn=None, token=True):
        c = conn or self.conn()
        h = {"X-Session-Token": self.wb.token} if token else {}
        h.update(headers or {})
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
            h.setdefault("Content-Type", "application/json")
        c.request(method, path, body=body, headers=h)
        r = c.getresponse()
        data = r.read()
        try:
            parsed = json.loads(data)
        except ValueError:
            parsed = data
        return r.status, parsed, r

    def wait(self, job_id):
        for _ in range(600):
            s, job, _ = self.call("GET", f"/api/v1/jobs/{quote(job_id)}")
            if job["state"] in ("SUCCEEDED", "FAILED", "CANCELLED"):
                return job
            threading.Event().wait(0.05)
        raise TimeoutError


class Security(ServerCase):
    def test_token_host_and_origin_are_enforced(self):
        self.assertEqual(self.call("GET", "/api/v1/overview", token=False)[0], 401)
        self.assertEqual(self.call("GET", "/api/v1/overview", headers={"X-Session-Token": "wrong"}, token=False)[0], 401)
        self.assertEqual(self.call("GET", "/api/v1/overview", headers={"Host": "evil.example"})[0], 421)       # DNS rebinding
        self.assertEqual(self.call("GET", "/api/v1/overview", headers={"Origin": "http://evil.example"})[0], 403)
        self.assertEqual(self.call("POST", "/api/v1/runs", {}, headers={"Origin": "https://evil.example"})[0], 403)
        self.assertEqual(self.call("GET", "/api/v1/overview")[0], 200)

    def test_no_cors_and_security_headers(self):
        s, _, r = self.call("GET", "/")
        self.assertEqual(s, 200)
        self.assertNotIn("Access-Control-Allow-Origin", r.headers)
        csp = r.headers["Content-Security-Policy"]
        self.assertIn("default-src 'none'", csp)
        self.assertIn("script-src 'self'", csp)
        self.assertNotIn("unsafe-inline", csp)
        self.assertEqual(r.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(r.headers["Cache-Control"], "no-store")

    def test_ui_assets_have_no_inline_script_and_no_remote_references(self):
        _, html, _ = self.call("GET", "/")
        text = html.decode()
        self.assertNotIn("__TOKEN__", text)
        self.assertIn(self.wb.token, text)
        self.assertEqual(text.count("<script"), 1)
        self.assertIn('src="/static/app.js"', text)
        self.assertNotIn("http://", text.replace("http://www.w3.org", ""))
        _, js, _ = self.call("GET", "/static/app.js")
        self.assertNotIn(b"innerHTML", js)
        self.assertNotIn(b"eval(", js)
        self.assertNotIn(b"document.write", js)

    def test_loopback_only_binding(self):
        with self.assertRaises(ValueError):
            Workbench(self.svc, "0.0.0.0", 0)

    def test_json_content_type_and_body_limits(self):
        self.assertEqual(self.call("POST", "/api/v1/runs", b"{}", headers={"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.call("POST", "/api/v1/runs", b"not json", headers={"Content-Type": "application/json"})[0], 400)
        c = self.conn()
        c.putrequest("POST", "/api/v1/sources")
        for k, v in {"X-Session-Token": self.wb.token, "Content-Length": str(10 ** 9), "X-Filename": "a.md"}.items():
            c.putheader(k, v)
        c.endheaders()
        self.assertEqual(c.getresponse().status, 413)

    def test_unknown_routes_and_methods(self):
        self.assertEqual(self.call("GET", "/api/v1/nope")[0], 404)
        self.assertEqual(self.call("GET", "/../../etc/passwd")[0], 404)
        self.assertEqual(self.call("GET", "/static/../server.py")[0], 404)


class HttpWorkflow(ServerCase):
    def test_import_review_run_export_over_http_with_keepalive(self):
        c = self.conn()                               # one persistent connection: framing must survive unread bodies
        ids = {}
        for name in ("product-spec.md", "service-config.json"):
            s, res, _ = self.call("POST", "/api/v1/sources", (TUTORIAL / name).read_bytes(),
                                  headers={"X-Filename": quote(name), "Content-Type": "application/octet-stream"}, conn=c)
            self.assertEqual(s, 202, res)
            self.assertEqual(self.wait(res["job_id"])["state"], "SUCCEEDED")
            ids[name] = res["source"]["id"]
        s, lst, _ = self.call("GET", "/api/v1/candidates?source_id=" + quote(ids["product-spec.md"]) + "&q=limits.max", conn=c)
        cand = lst["items"][0]
        s, one, r = self.call("GET", "/api/v1/candidates/" + quote(cand["id"]), conn=c)
        etag = r.headers["ETag"]
        s, _, _ = self.call("POST", f"/api/v1/candidates/{quote(cand['id'])}/decisions", {"action": "APPROVE"}, conn=c)
        self.assertEqual(s, 428)
        s, dec, _ = self.call("POST", f"/api/v1/candidates/{quote(cand['id'])}/decisions", {"action": "APPROVE"},
                              headers={"If-Match": etag}, conn=c)
        self.assertEqual((s, dec["status"]), (200, "APPROVED"))
        s, err, r = self.call("POST", f"/api/v1/candidates/{quote(cand['id'])}/decisions", {"action": "REJECT", "reason": "x"},
                              headers={"If-Match": etag}, conn=c)
        self.assertEqual((s, err["error"]["code"]), (412, "precondition_failed"))
        s, run, _ = self.call("POST", "/api/v1/runs", {"claim_revision_ids": [dec["claim_revision_id"]],
                              "evidence_source_ids": [ids["service-config.json"]]}, headers={"Idempotency-Key": "k"}, conn=c)
        self.assertEqual(s, 202)
        self.assertEqual(self.wait(run["job_id"])["state"], "SUCCEEDED")
        s, view, _ = self.call("GET", "/api/v1/runs/" + quote(run["run_id"]), conn=c)
        self.assertEqual(view["findings"][0]["verdict"], "CONTRADICTED")
        s, z, r = self.call("GET", f"/api/v1/runs/{quote(run['run_id'])}/export", conn=c)
        self.assertEqual((s, r.headers["Content-Type"]), (200, "application/zip"))
        s, ver, _ = self.call("POST", f"/api/v1/runs/{quote(run['run_id'])}/verify", {}, conn=c)       # empty-body POST, same connection
        self.assertEqual(ver["status"], "VERIFIED")
        s, ov, _ = self.call("GET", "/api/v1/overview", conn=c)
        self.assertEqual(ov["latest_run"]["freshness"]["state"], "CURRENT")

    def test_hostile_markdown_stays_inert_through_the_api(self):
        evil = b"---\nk: \"<img src=x onerror=alert(1)>\"\n---\n<script>alert(1)</script>\n"
        s, res, _ = self.call("POST", "/api/v1/sources", evil, headers={"X-Filename": "evil.md"})
        self.wait(res["job_id"])
        s, txt, r = self.call("GET", f"/api/v1/sources/{quote(res['source']['id'])}/text")
        self.assertTrue(r.headers["Content-Type"].startswith("application/json"))
        self.assertIn("<script>", txt["text"])            # preserved verbatim as text; the UI only uses textContent


if __name__ == "__main__":
    unittest.main()

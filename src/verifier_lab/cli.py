"""Command line client over the same application service as the UI (JSON on stdout, diagnostics on stderr).

Exit codes: 0 success; 1 completed with findings that need attention; 2 invalid input;
3 package rejected by the independent checker; 4 operational failure.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .canonical import CanonicalError
from .service import Service, ServiceError
from .validation import parse_bundle

OK, ATTENTION, INVALID, REJECTED, FAILED = 0, 1, 2, 3, 4


def _out(obj) -> None:
    print(json.dumps(obj, indent=2, sort_keys=True))


def _err(msg: str) -> None:
    print(msg, file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="verifier-lab", description=__doc__.split("\n\n")[0])
    ap.add_argument("--workspace", default=".workspace", help="local workspace directory (default: .workspace)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="start the local workbench UI (loopback only)")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8765)
    s = sub.add_parser("import", help="import PDF/JSON/Markdown/SQL files")
    s.add_argument("files", nargs="+")
    s.add_argument("--dialect", default=None)
    s = sub.add_parser("candidates", help="list candidate claims")
    s.add_argument("--source")
    s.add_argument("--status")
    s = sub.add_parser("approve", help="approve candidates as claims")
    s.add_argument("ids", nargs="+")
    s = sub.add_parser("reject", help="reject candidates (reason required)")
    s.add_argument("ids", nargs="+")
    s.add_argument("--reason", required=True)
    s = sub.add_parser("check", help="run checks of approved claims against evidence sources")
    s.add_argument("--claims", nargs="*", default=None, help="claim revision ids (default: all current)")
    s.add_argument("--evidence", nargs="+", required=True, help="source ids to test against")
    s = sub.add_parser("export", help="export a run as a portable package (zip)")
    s.add_argument("run_id")
    s.add_argument("--output", required=True)
    s = sub.add_parser("verify-package", help="independently verify an exported package (dir or zip)")
    s.add_argument("package")
    s = sub.add_parser("validate", help="structurally validate a bundle.json")
    s.add_argument("bundle")
    args = ap.parse_args(argv)

    if args.cmd == "verify-package":
        from .checker import verify_package
        res = verify_package(Path(args.package))
        _out(res)
        return OK if res["status"] == "VERIFIED" else REJECTED
    if args.cmd == "validate":
        bundle, diags = parse_bundle(Path(args.bundle).read_bytes())
        _out({"valid": bundle is not None, "bundle_id": bundle.bundle_id if bundle else None,
              "diagnostics": [d.to_dict() for d in diags]})
        return OK if bundle else INVALID
    if args.cmd == "serve":
        from .server import serve
        try:
            wb = serve(Path(args.workspace), args.host, args.port)
        except ValueError as exc:
            _err(str(exc))
            return INVALID
        _err(f"Workbench: {wb.url}  (loopback only; Ctrl+C to stop)")
        try:
            wb.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            wb.service.close()
        return OK
    svc = Service(Path(args.workspace))
    try:
        if args.cmd == "import":
            results = []
            for f in args.files:
                p = Path(f)
                res = svc.import_source(p.read_bytes(), p.name, None, args.dialect)
                if res["job_id"]:
                    job = svc.wait_job(res["job_id"])
                    res["job"] = job
                res["source"] = svc.get_source(res["source"]["id"])
                results.append(res)
            _out(results)
            return OK if all((r.get("job") or {"state": "SUCCEEDED"})["state"] == "SUCCEEDED" for r in results) else FAILED
        if args.cmd == "candidates":
            _out(svc.list_candidates(source_id=args.source, status=args.status, limit=500))
            return OK
        if args.cmd in ("approve", "reject"):
            out = []
            for cid in args.ids:
                c = svc.get_candidate(cid)
                out.append(svc.decide(cid, "APPROVE" if args.cmd == "approve" else "REJECT", c["etag"],
                                      reason=getattr(args, "reason", None)))
            _out([{"id": c["id"], "status": c["status"], "claim_revision_id": c["claim_revision_id"]} for c in out])
            return OK
        if args.cmd == "check":
            claims = args.claims or [c["id"] for c in svc.list_claims() if c["status"] == "CURRENT"]
            started = svc.start_run(claims, args.evidence)
            job = svc.wait_job(started["job_id"], timeout=3600)
            run = svc.get_run(started["run_id"])
            _out(run)
            if job["state"] != "SUCCEEDED":
                return FAILED
            return OK if all(f["verdict"] == "SUPPORTED" for f in run["findings"]) else ATTENTION
        if args.cmd == "export":
            out = Path(args.output)
            if out.exists():
                _err(f"refusing to overwrite {out}")
                return INVALID
            src = svc.export_run(args.run_id)
            out.write_bytes(src.read_bytes())
            _out({"exported": str(out)})
            return OK
    except ServiceError as exc:
        _err(f"{exc.code}: {exc.message}")
        _out(exc.to_dict())
        return INVALID if exc.status < 500 else FAILED
    except (CanonicalError, OSError) as exc:
        _err(str(exc))
        return FAILED
    finally:
        svc.close()
    return OK


if __name__ == "__main__":
    sys.exit(main())

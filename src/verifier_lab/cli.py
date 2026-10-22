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


def _experiments(args) -> int:
    if (args.cmd == "experiment" and args.kind in ("pipeline", "scheduler")) or \
            (args.cmd == "minimize" and args.system != "store"):
        return _laboratory(args)
    from .experiments import crash as crash_mod
    from .experiments.checkpoint import ExperimentCase, run_experiment
    from .experiments.history import generate_history
    from .experiments.package import (CheckpointOracle, build_package, verify_package_fresh_process, write_package)
    from .minimize import ReductionBudget, minimize
    from .reducers.sequence import DeleteTransactionMembers, DeleteUnits
    from .reducers.values import SimplifyValues
    from .subjects.candidate_store import DEFECTS
    import shlex
    from .subjects.external import ExternalFactory
    cmd = shlex.split(args.subject_cmd) if getattr(args, "subject_cmd", None) else None
    env = dict(i.partition("=")[::2] for i in getattr(args, "subject_env", []))
    factory = ExternalFactory(cmd, env=env) if cmd else None
    cmd = factory.command if factory else None       # file arguments resolved to absolute paths before any child process starts
    if args.cmd == "verify-counterexample":
        res = verify_package_fresh_process(Path(args.package), command=cmd, env=env)
        _out(res)
        return OK if res["status"] == "VERIFIED" else REJECTED
    if args.cmd == "experiment" and args.kind == "crash":
        res = crash_mod.crash_experiment(args.writer_defect, tuple(args.defect), args.max_positions)
        _out(res.to_dict())
        return OK if res.passed else ATTENTION
    if factory and args.defect:
        _err("--defect only applies to the built-in candidate")
        return INVALID
    if args.cmd == "minimize" and not factory and not args.defect:
        _err("minimize needs --defect (built-in) or --subject-cmd")
        return INVALID
    bad = [d for d in args.defect if d not in DEFECTS]
    if bad:
        _err(f"unknown defect(s) {bad}; known: {list(DEFECTS)}")
        return INVALID
    ops = generate_history(args.seed, args.ops)
    boundary = args.ops // 2 if args.boundary is None else args.boundary
    case = ExperimentCase(args.seed, tuple(ops), boundary, tuple(args.defect))
    if args.cmd == "experiment":
        res = run_experiment(case, factory)
        _out({"case": case.identity(), "subject": factory.identity if factory else "builtin", **res.to_dict()})
        return OK if res.passed else ATTENTION
    out = Path(args.output)
    if out.exists():
        _err(f"refusing to overwrite {out}")
        return INVALID
    reducers = [DeleteUnits(), DeleteTransactionMembers(), SimplifyValues()]
    result = minimize(case, CheckpointOracle(factory=factory), reducers, ReductionBudget(max_oracle_calls=args.max_oracle_calls))
    if result.status in ("NOT_REPRODUCED", "UNSTABLE"):
        _err(f"nothing to minimize: {result.stop_reason}")
        _out({"status": result.status, "reproduction_outcomes": result.reproduction_outcomes})
        return INVALID
    pkg = build_package(result, reducers, factory)
    write_package(pkg, out)
    verdict = verify_package_fresh_process(out, command=cmd, env=env)
    _out({"status": result.status, "original_units": len(case.ops), "reduced_units": len(result.best.ops),
          "oracle_calls": result.oracle_calls, "scope": pkg["reduction"]["scope"],
          "fresh_process_verification": verdict["status"], "package": str(out)})
    return OK if verdict["status"] == "VERIFIED" else REJECTED


def _laboratory(args) -> int:
    from .canonical import canonical_bytes
    from .experiments.laboratory import generated_case, reduce_case, run_case
    from .experiments.package import verify_package_fresh_process, write_package
    if args.subject_cmd or args.subject_env:
        _err("pipeline and scheduler experiments use only the installed fictional subjects")
        return INVALID
    try:
        case = generated_case(args.kind if args.cmd == "experiment" else args.system,
                              args.seed, args.ops, args.boundary, args.defect)
        if args.cmd == "experiment":
            result = run_case(case)
            _out({"case": case.identity(), **result.to_dict()})
            return FAILED if result.errors else OK if result.passed else ATTENTION
        target = Path(args.output)
        if target.exists():
            _err(f"refusing to overwrite {target}")
            return INVALID
        summary, package = reduce_case(case, args.max_oracle_calls)
        if package is None:
            _out(summary)
            return INVALID
        write_package(package, target)
        checked = verify_package_fresh_process(target)
        _out({**summary, "fresh_process_verification": checked["status"], "package": str(target)})
        return OK if checked["status"] == "VERIFIED" else REJECTED
    except (ValueError, TypeError) as exc:
        _err(str(exc))
        return INVALID
    except OSError as exc:
        _err(str(exc))
        return FAILED


def _campaign(args) -> int:
    from . import campaign_checker
    from .harness import campaign
    if args.action == "verify":
        if not (args.report and args.manifest):
            _err("verify needs --report and --manifest")
            return INVALID
        res = campaign_checker.verify_campaign_report(json.loads(Path(args.report).read_text()), json.loads(Path(args.manifest).read_text()),
                                                      args.trusted_manifest_digest)
        _out(res)
        return OK if res["status"] == "ACCEPTED" else REJECTED
    from .harness.world import KERNELS
    kernel = args.kernel
    if args.kernel_cmd:
        import shlex
        from .harness.external_kernel import ExternalKernel
        kernel = ExternalKernel(shlex.split(args.kernel_cmd), env=dict(i.partition("=")[::2] for i in args.kernel_env))
    elif args.kernel not in KERNELS:
        _err(f"unknown kernel; known: {sorted(KERNELS)}")
        return INVALID
    outs = [Path(p) for p in (args.output, args.manifest_output) if p]
    if any(p.exists() for p in outs):
        _err("refusing to overwrite an existing output")
        return INVALID
    manifest = campaign.build_manifest(args.seed)
    report = campaign.run_campaign(kernel, manifest)
    if args.output:
        Path(args.output).write_text(json.dumps(report, indent=1, sort_keys=True))
    if args.manifest_output:
        Path(args.manifest_output).write_text(json.dumps(manifest, indent=1, sort_keys=True))
    _out({"kernel": report["kernel"]["name"], "manifest_digest": manifest["manifest_digest"], "report_digest": report["report_digest"],
          "summary": report["summary"]["counts"], "mutation_detection": report["summary"]["mutation_detection"],
          "survivors": report["survivors"]})
    return OK if not report["survivors"] and report["summary"]["counts"]["ERROR"] == 0 else ATTENTION


def _packs(args) -> int:
    from . import riskpacks as RP
    from .rules import builtin_registry
    reg = builtin_registry()
    catalog = RP.PackCatalog(Path(args.workspace) / "packs", reg.implemented_predicates())
    try:
        if args.action == "list":
            _out([{"pack_id": p.pack_id, "version": p.version, "digest": p.content_digest,
                   "obligations": len(p.obligations)} for p in catalog.list()])
        elif args.action == "validate":
            for f in args.files:
                pack = RP.load_pack(Path(f).read_bytes(), reg.implemented_predicates())
                _out({"valid": True, "pack": pack.profile_ref()})
        elif args.action == "install":
            _out([catalog.install(Path(f).read_bytes()).profile_ref() for f in args.files])
        else:
            if len(args.files) != 2:
                _err("diff needs two pack files")
                return INVALID
            a, b = (RP.load_pack(Path(f).read_bytes(), reg.implemented_predicates()) for f in args.files)
            _out(RP.diff_packs(a, b))
    except RP.PackError as exc:
        _err(str(exc))
        _out({"valid": False, "error": {"code": exc.code, "message": str(exc)}})
        return INVALID
    return OK


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="verifier-lab", description=__doc__.split("\n\n")[0])
    ap.add_argument("--workspace", default=".workspace", help="local workspace directory (default: .workspace)")
    ap.add_argument("--parser-isolation", choices=["required", "portable"], default="required",
                    help="required Linux sandbox (default), or explicit portable development mode")
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
    s = sub.add_parser("source", help="inspect a source and its inert text rendition")
    s.add_argument("source_id")
    s = sub.add_parser("author", help="propose a source-linked human interpretation (requires separate approval)")
    s.add_argument("source_id")
    s.add_argument("--attempt", required=True, help="current extraction attempt from the source command")
    s.add_argument("--start", required=True, type=int, help="UTF-8 byte offset in the text rendition")
    s.add_argument("--end", required=True, type=int, help="exclusive UTF-8 byte offset")
    s.add_argument("--predicate", default="fact.equals")
    s.add_argument("--subject", required=True)
    s.add_argument("--arguments", required=True, help="JSON object with the predicate arguments")
    s.add_argument("--reason", required=True)
    s.add_argument("--idempotency-key")
    s = sub.add_parser("approve", help="approve candidates as claims")
    s.add_argument("ids", nargs="+")
    s = sub.add_parser("reject", help="reject candidates (reason required)")
    s.add_argument("ids", nargs="+")
    s.add_argument("--reason", required=True)
    s = sub.add_parser("check", help="run checks of approved claims against evidence sources")
    s.add_argument("--claims", nargs="*", default=None, help="claim revision ids (default: all current)")
    s.add_argument("--evidence", nargs="+", required=True, help="source ids to test against")
    s.add_argument("--pack", action="append", default=[], help="risk pack id to assess (repeatable)")
    s.add_argument("--context", action="append", default=[], metavar="KEY=VALUE",
                   help="applicability fact (true/false/null/integer/text); unknown facts stay UNKNOWN")
    s.add_argument("--context-file", help="JSON object of applicability facts")
    s = sub.add_parser("pack", help="inspect, validate, install and diff risk packs (data only)")
    s.add_argument("action", choices=["list", "validate", "install", "diff"])
    s.add_argument("files", nargs="*")
    s = sub.add_parser("export", help="export a run as a portable package (zip)")
    s.add_argument("run_id")
    s.add_argument("--output", required=True)
    s = sub.add_parser("verify-package", help="independently verify an exported package (dir or zip)")
    s.add_argument("package")
    s = sub.add_parser("experiment", help="run a replay/checkpoint or crash experiment on the reference store")
    s.add_argument("kind", choices=["checkpoint", "crash", "pipeline", "scheduler"])
    s.add_argument("--seed", default="seed-1")
    s.add_argument("--ops", type=int, default=60)
    s.add_argument("--boundary", type=int, default=None, help="checkpoint position (default: half)")
    s.add_argument("--defect", action="append", default=[], help="inject a named defect (positive control)")
    s.add_argument("--writer-defect", default=None)
    s.add_argument("--max-positions", type=int, default=None)
    s.add_argument("--subject-cmd", help="run your own subject program (JSON-lines protocol) instead of the built-in candidate")
    s.add_argument("--subject-env", action="append", default=[], metavar="KEY=VALUE", help="environment passed to the subject (its identity includes it)")
    s = sub.add_parser("minimize", help="reduce a failing checkpoint experiment to a small verified reproducer")
    s.add_argument("--system", choices=["store", "pipeline", "scheduler"], default="store")
    s.add_argument("--seed", default="seed-1")
    s.add_argument("--ops", type=int, default=120)
    s.add_argument("--boundary", type=int, default=None)
    s.add_argument("--defect", action="append", default=[])
    s.add_argument("--subject-cmd", help="minimize a failure of your own subject program")
    s.add_argument("--subject-env", action="append", default=[], metavar="KEY=VALUE")
    s.add_argument("--max-oracle-calls", type=int, default=3000)
    s.add_argument("--output", required=True)
    s = sub.add_parser("campaign", help="run a mutation/metamorphic campaign, or verify a campaign report")
    s.add_argument("action", choices=["run", "verify"])
    s.add_argument("--kernel", default="real")
    s.add_argument("--kernel-cmd", help="judge YOUR verifier program (JSON-lines protocol) instead of a built-in kernel")
    s.add_argument("--kernel-env", action="append", default=[], metavar="KEY=VALUE")
    s.add_argument("--seed", default="campaign-1")
    s.add_argument("--output", help="report path (run)")
    s.add_argument("--manifest-output", help="manifest path (run)")
    s.add_argument("--report")
    s.add_argument("--manifest")
    s.add_argument("--trusted-manifest-digest", help="digest from a trusted channel; without it the conclusion is narrower")
    s = sub.add_parser("verify-counterexample", help="reproduce a counterexample package in a fresh process")
    s.add_argument("package")
    s.add_argument("--subject-cmd", help="required for packages that name an external subject; its identity must match")
    s.add_argument("--subject-env", action="append", default=[], metavar="KEY=VALUE")
    s = sub.add_parser("validate", help="structurally validate a bundle.json")
    s.add_argument("bundle")
    args = ap.parse_args(argv)

    if args.cmd == "verify-package":
        from .checker import verify_package
        res = verify_package(Path(args.package))
        _out(res)
        return OK if res["status"] == "VERIFIED" else REJECTED
    if args.cmd == "campaign":
        return _campaign(args)
    if args.cmd in ("experiment", "minimize", "verify-counterexample"):
        return _experiments(args)
    if args.cmd == "pack":
        return _packs(args)
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
    from .ingestion.common import Limits
    svc = Service(Path(args.workspace), limits=Limits(isolation=args.parser_isolation))
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
        if args.cmd == "source":
            _out({"source": svc.get_source(args.source_id, detail=True), "rendition": svc.source_text(args.source_id)})
            return OK
        if args.cmd == "author":
            try:
                arguments = json.loads(args.arguments)
            except ValueError:
                raise ServiceError(422, "bad_arguments", "arguments must be a JSON object")
            _out(svc.author_candidate(args.source_id, args.attempt, args.start, args.end,
                 {"predicate": args.predicate, "subject": args.subject, "arguments": arguments}, args.reason,
                 idempotency_key=args.idempotency_key))
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
            facts = json.loads(Path(args.context_file).read_text()) if args.context_file else {}
            for item in args.context:
                k, _, v = item.partition("=")
                facts[k] = ({"true": True, "false": False, "null": None}.get(v, int(v) if v.lstrip("-").isdigit() else v))
            started = svc.start_run(claims, args.evidence, pack_ids=args.pack, context=facts)
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

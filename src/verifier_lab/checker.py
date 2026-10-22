"""Clean-room package checker (chapter 10). Deliberately imports NOTHING from the producer.

It shares only the written package format: it re-hashes bytes, re-derives identities, slices cited
tokens out of admitted artifact bytes and re-derives each verdict with its own small decision table.
Meta-verification is not a domain verdict: "rejected" means the package was not accepted.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import sys
import zipfile
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath

KNOWN_RULES = {("rule.fact_equals", "1"): "fact.equals",
               ("rule.fact_numeric_bound", "1"): "fact.numeric_bound",
               ("rule.fact_present", "1"): "fact.present",
               ("dispatch.unsupported", "1"): None}
EXTRACTION_MEDIA = "application/vnd.verifier-lab.extraction-record+json"
PROVENANCE_MEDIA = "application/vnd.verifier-lab.claim-provenance+json"
LIMITS = ["Completeness of facts inside each extraction record is not re-derived: re-parsing the "
          "original PDF/JSON/Markdown/SQL is out of this checker's scope, so a fact the extractor "
          "never recorded cannot be detected here.",
          "Fact keys are verified against cited token context only, not by re-walking source structure.",
          "PDF text renditions are verified as bytes; their derivation from the PDF is not reproduced.",
          "Hashes prove byte consistency, not authorship; no signatures or trust anchors are evaluated."]
MAX_FILE = 200_000_000


class Reject(Exception):
    pass


def _canon(v) -> bytes:
    def check(x, d=0):
        if d > 64:
            raise Reject("depth")
        if isinstance(x, float):
            raise Reject("float in normative record")
        if isinstance(x, dict):
            for k, y in x.items():
                if not isinstance(k, str):
                    raise Reject("non-string key")
                check(y, d + 1)
        elif isinstance(x, list):
            for y in x:
                check(y, d + 1)
    check(v)
    return json.dumps(v, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def _strict(data: bytes):
    def pairs(p):
        d = {}
        for k, v in p:
            if k in d:
                raise Reject(f"duplicate key {k!r}")
            d[k] = v
        return d

    def bad(*_):
        raise Reject("float or non-finite number")
    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=pairs, parse_float=bad, parse_constant=bad)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise Reject(f"unparseable JSON: {type(exc).__name__}")


def _rid(kind: str, body) -> str:
    return kind + ":sha256:" + hashlib.sha256(kind.encode() + b"\x00" + _canon(body)).hexdigest()


def _dg(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _safe(path: str) -> bool:
    if not path or "\x00" in path or "\\" in path or path.startswith("/") or (len(path) > 1 and path[1] == ":"):
        return False
    parts = PurePosixPath(path).parts
    return bool(parts) and "/".join(parts) == path and all(p not in ("", ".", "..") for p in parts)


# ------------------------------------------------------------------ value semantics (own copy)
_INT = re.compile(r"-?(?:0|[1-9][0-9]*)\Z")
_DEC = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?\Z")


def _num(tok: str):
    if _INT.match(tok):
        return {"kind": "integer", "value": tok}
    if _DEC.match(tok):
        return {"kind": "decimal", "value": tok}
    return None


def derive_value(grammar: str, token: str):
    if grammar == "json-literal":
        if token in ("true", "false", "null"):
            return {"kind": "boolean" if token != "null" else "null",
                    "value": {"true": True, "false": False, "null": None}[token]}
        if token.startswith('"'):
            s = json.loads(token)
            return {"kind": "string", "value": s}
        return _num(token) or Reject("not a JSON literal")
    if grammar == "plain-scalar":
        t = token.strip()
        if len(t) >= 2 and t[0] == t[-1] and t[0] in "\"'":
            return {"kind": "string", "value": t[1:-1]}
        if t == "true":
            return {"kind": "boolean", "value": True}
        if t == "false":
            return {"kind": "boolean", "value": False}
        if t == "null":
            return {"kind": "null", "value": None}
        return _num(t) or {"kind": "string", "value": t}
    if grammar == "sql-literal":
        t = token.strip()
        if len(t) >= 2 and t[0] == t[-1] == "'":
            return {"kind": "string", "value": t[1:-1].replace("''", "'")}
        if t.upper() == "NULL":
            return {"kind": "null", "value": None}
        if t.upper() in ("TRUE", "FALSE"):
            return {"kind": "boolean", "value": t.upper() == "TRUE"}
        return _num(t) or {"kind": "string", "value": t}
    if grammar == "sql-flag":
        if re.sub(r"\s+", " ", token.strip().upper()) in ("NOT NULL", "PRIMARY KEY", "UNIQUE"):
            return {"kind": "boolean", "value": True}
    raise Reject(f"unknown grammar {grammar!r}")


def _numeric(v):
    return isinstance(v, dict) and v.get("kind") in ("integer", "decimal")


def _eq(a, b):
    if _numeric(a) and _numeric(b):
        return Decimal(a["value"]) == Decimal(b["value"])
    if a["kind"] != b["kind"]:
        return None
    return a["value"] == b["value"]


# ------------------------------------------------------------------------------- loading
def load_package(path: Path) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    if path.is_dir():
        for p in sorted(path.rglob("*")):
            if p.is_symlink():
                raise Reject(f"symlink in package: {p.name}")
            if p.is_file():
                rel = p.relative_to(path).as_posix()
                if p.stat().st_size > MAX_FILE:
                    raise Reject("file too large")
                files[rel] = p.read_bytes()
        return files
    try:
        with zipfile.ZipFile(path) as zf:
            infos = zf.infolist()
            seen = set()
            total = 0
            for info in infos:
                if info.is_dir():
                    continue
                if not _safe(info.filename) or info.filename in seen:
                    raise Reject(f"unsafe or duplicate archive member {info.filename!r}")
                seen.add(info.filename)
                if info.file_size > MAX_FILE:
                    raise Reject("archive member too large")
                total += info.file_size
                if total > 4 * MAX_FILE:
                    raise Reject("archive expansion limit")
                data = zf.read(info)
                if len(data) != info.file_size:
                    raise Reject("archive size mismatch")
                files[info.filename] = data
    except zipfile.BadZipFile:
        raise Reject("not a directory or valid zip package")
    return files


# --------------------------------------------------------------------------------- checks
class Report:
    def __init__(self):
        self.checks: list[dict] = []
        self.findings: list[dict] = []
        self.unsupported: list[dict] = []

    def check(self, name: str, ok: bool, detail: str = ""):
        self.checks.append({"name": name, "ok": bool(ok), "detail": detail})
        return ok

    @property
    def ok(self) -> bool:
        return all(c["ok"] for c in self.checks) and all(f["accepted"] for f in self.findings)


def verify_package(path: Path) -> dict:
    rep = Report()
    try:
        _verify(Path(path), rep)
    except Reject as exc:
        rep.check("package_readable", False, str(exc))
    except Exception as exc:  # never crash on hostile input; report as rejected
        rep.check("checker_internal_error", False, f"{type(exc).__name__}: {exc}")
    return {"status": "VERIFIED" if rep.ok else "REJECTED", "checks": rep.checks,
            "findings": rep.findings, "unsupported": rep.unsupported, "limitations": LIMITS,
            "note": "meta-verification only: VERIFIED means the package is internally consistent and "
                    "its findings re-derive; it does not mean the claims are true."}


def _verify(path: Path, rep: Report):
    files = load_package(path)
    # 1. manifest integrity
    if "MANIFEST.json" not in files:
        raise Reject("MANIFEST.json missing")
    manifest = _strict(files["MANIFEST.json"])
    listed = {}
    for entry in manifest.get("files", []):
        if not _safe(entry.get("path", "")) or entry["path"] in listed:
            raise Reject("manifest has unsafe or duplicate path")
        listed[entry["path"]] = entry
    for rel, entry in listed.items():
        data = files.get(rel)
        if data is None:
            rep.check(f"file_present:{rel}", False, "listed in manifest but missing")
        elif _dg(data) != entry["sha256"] or len(data) != entry["size"]:
            rep.check(f"file_hash:{rel}", False, "bytes differ from manifest")
    observed = set(manifest.get("observation_files", []))
    for rel in observed:  # non-semantic metadata: must exist and parse, but is never trusted for verdicts
        if rel not in files:
            rep.check(f"file_present:{rel}", False, "listed observation file missing")
        else:
            try:
                json.loads(files[rel].decode("utf-8"))   # metadata may hold floats; it never feeds a verdict
            except (UnicodeDecodeError, ValueError):
                rep.check(f"observation_file:{rel}", False, "not valid JSON")
    extra = sorted(set(files) - set(listed) - observed - {"MANIFEST.json"})
    rep.check("no_unlisted_files", not extra, ", ".join(extra[:5]))
    if not rep.ok:
        return
    rep.check("manifest_hashes", True, f"{len(listed)} files")
    # 2. bundle structure and identities
    bundle = _strict(files["bundle.json"])
    top = {"schema_version", "bundle_id", "artifacts", "claims", "activities", "assumptions", "profile_refs"}
    if set(bundle) != top or bundle["schema_version"] != "1":
        raise Reject("bundle fields/version unsupported")
    body = {k: v for k, v in bundle.items() if k != "bundle_id"}
    rep.check("bundle_id", _rid("bundle", body) == bundle["bundle_id"])
    arts, claims, acts, asms = {}, {}, {}, {}
    for a in bundle["artifacts"]:
        b = {k: v for k, v in a.items() if k != "id"}
        ok = _rid("artifact", b) == a["id"] and _safe(a["relative_path"])
        data = files.get(a["relative_path"])
        ok = ok and data is not None and _dg(data) == a["digest"] and len(data) == a["size_bytes"]
        if not rep.check(f"artifact:{a['relative_path'][-16:]}", ok, "identity/path/digest"):
            continue
        arts[a["id"]] = a
    for kind, items, dest in (("claim", bundle["claims"], claims), ("activity", bundle["activities"], acts),
                              ("assumption", bundle["assumptions"], asms)):
        for r in items:
            ok = _rid(kind, {k: v for k, v in r.items() if k != "id"}) == r["id"]
            if rep.check(f"{kind}_id:{r['id'][-8:]}", ok):
                dest[r["id"]] = r
    for c in claims.values():
        for ref in c["evidence_refs"]:
            rep.check(f"claim_ref:{c['id'][-8:]}", ref in arts or ref in acts, f"dangling {ref}")
        for ref in c["assumption_refs"]:
            rep.check(f"claim_assumption_ref:{c['id'][-8:]}", ref in asms)
    if not rep.ok:
        return
    # 3. extraction records and provenance
    records = _load_records(files, arts, acts, rep)
    # 4. result consistency
    result = _strict(files["result.json"])
    rep.check("result_bundle_binding", result.get("bundle_id") == bundle["bundle_id"])
    comp = result.get("completion", {})
    rep.check("run_complete", comp.get("claims_completed") == comp.get("claims_total") == len(claims)
              and not comp.get("cancelled") and not result.get("errors"),
              "package must cover every claim without execution errors")
    claim_index = {c["claim_id"]: c for c in result.get("claims", [])}
    seen_claims = set()
    for cid, c in claims.items():
        _check_provenance(c, files, arts, rep)
    for f in result["findings"]:
        entry = {"finding_id": f.get("id"), "accepted": False, "checked": "re-derived", "detail": ""}
        rep.findings.append(entry)
        try:
            _check_finding(f, claims, arts, acts, records, files, entry, rep)
        except Reject as exc:
            entry["accepted"], entry["detail"] = False, str(exc)
        seen_claims.add(f.get("claim_id"))
    missing = set(claims) - seen_claims
    rep.check("every_claim_has_a_finding", not missing, f"{len(missing)} claims without findings")
    rep.check("claim_index_complete", set(claim_index) == set(claims))


def _load_records(files, arts, acts, rep):
    records = {}  # source artifact id -> (record, rendition artifact id)
    for act in acts.values():
        if act["operation"] != "ingestion.extract":
            continue
        for out in act["outputs"]:
            a = arts.get(out)
            if a and a["media_type"] == EXTRACTION_MEDIA:
                rec = _strict(files[a["relative_path"]])
                rend = next((x for x in act["outputs"] + act["inputs"] if arts[x]["digest"] == rec["rendition_digest"]), None)
                src_ok = all(arts[i]["digest"] == rec["source_digest"] for i in act["inputs"])
                rep.check(f"extraction_binding:{out[-8:]}", rend is not None and src_ok)
                for i in act["inputs"]:
                    records[i] = (rec, rend)
    return records


def _slice(files, arts, rend_id, start, end) -> bytes:
    a = arts.get(rend_id)
    if a is None:
        raise Reject("rendition artifact missing")
    data = files[a["relative_path"]]
    if not (0 <= start <= end <= len(data)):
        raise Reject("locator outside rendition")
    return data[start:end]


def _verify_fact(files, arts, rend_id, fact) -> dict:
    loc = fact["locator"]
    token = _slice(files, arts, rend_id, loc["byte_start"], loc["byte_end"])
    if _dg(token) != fact["token_digest"]:
        raise Reject(f"token bytes differ from recorded digest at {loc['byte_start']}")
    try:
        value = derive_value(fact["grammar"], token.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise Reject(f"token does not parse under {fact['grammar']}: {exc}")
    if isinstance(value, Reject):
        raise value
    if value != fact["value"]:
        raise Reject(f"recorded value differs from token-derived value at {loc['byte_start']}")
    if "context_start" in loc:
        ctx = _slice(files, arts, rend_id, loc["context_start"], loc["context_end"]).decode("utf-8", "replace")
        last = re.split(r"[.\[\]]", fact["key"].rstrip("]"))[-1].strip('"')
        if last and not last.isdigit() and last.lower() not in ctx.lower() and not fact["key"].startswith("sql."):
            raise Reject("fact key not present in its cited context")
    return value


def _check_provenance(claim, files, arts, rep):
    prov_ref = next((r for r in claim["evidence_refs"] if r in arts and arts[r]["media_type"] == PROVENANCE_MEDIA), None)
    name = f"provenance:{claim['id'][-8:]}"
    if prov_ref is None:
        rep.check(name, False, "claim has no provenance artifact")
        return
    prov = _strict(files[arts[prov_ref]["relative_path"]])
    dec = prov.get("decision", {})
    ok = dec.get("action") in ("APPROVE", "APPROVE_WITH_EDIT") and bool(dec.get("id"))
    if dec.get("action") == "APPROVE_WITH_EDIT":
        ok = ok and bool((dec.get("reason") or "").strip()) and dec.get("edited") is True
    if not rep.check(name + ":review_decision", ok, "approved review decision with reason when edited"):
        return
    span = prov["origin_span"]
    origin = arts.get(prov["origin_source_artifact_id"])
    if not rep.check(name + ":origin_artifact", origin is not None):
        return
    rend_id = next((i for i, a in arts.items() if a["digest"] == span["rendition_digest"]), None)
    try:
        token = _slice(files, arts, rend_id, span["locator"]["byte_start"], span["locator"]["byte_end"])
        good = _dg(token) == span["text_digest"] and derive_value(span["grammar"], token.decode()) == span["value"]
    except (Reject, ValueError, UnicodeDecodeError):
        good = False
    rep.check(name + ":origin_span_bytes", good, "claim traces to immutable bytes")


def _check_finding(f, claims, arts, acts, records, files, entry, rep):
    claim = claims.get(f.get("claim_id"))
    if claim is None:
        raise Reject("finding names a claim not in the bundle")
    body = {k: v for k, v in f.items() if k != "id"}
    if _rid("finding", body) != f["id"]:
        raise Reject("finding id does not match its content")
    key = (f["rule_id"], f["rule_version"])
    if key not in KNOWN_RULES:
        entry["accepted"], entry["checked"] = True, "unsupported_rule_family"
        entry["detail"] = "rule not in this checker's catalog; not independently checked"
        rep.unsupported.append({"finding_id": f["id"], "rule": f"{key[0]}@{key[1]}"})
        return
    if not set(f["evidence_refs"]) <= set(claim["evidence_refs"]):
        raise Reject("finding cites evidence outside its claim")
    predicate = KNOWN_RULES[key]
    if predicate is None:
        if f["verdict"] != "INSUFFICIENT_EVIDENCE" or f["reason_code"] != "no_registered_rule":
            raise Reject("unexpected dispatch finding")
        entry["accepted"], entry["detail"] = True, "no rule for predicate (checker catalog also lacks it)" \
            if claim["predicate"] not in KNOWN_RULES.values() else ""
        if claim["predicate"] in KNOWN_RULES.values():
            entry["accepted"], entry["detail"] = False, "a rule exists for this predicate"
        return
    if claim["predicate"] != predicate:
        raise Reject("rule does not match claim predicate")
    verdict, reason, observations = _rederive(claim, predicate, arts, records, files)
    if (verdict, reason) != (f["verdict"], f["reason_code"]):
        raise Reject(f"re-derived {verdict}/{reason}, finding says {f['verdict']}/{f['reason_code']}")
    w = f.get("witness") or {}
    if f["verdict"] in ("SUPPORTED", "CONTRADICTED", "HUMAN_REVIEW_REQUIRED") and "observations" in w:
        theirs = sorted((o["artifact_id"], o["byte_start"], o["byte_end"], o["token_digest"]) for o in w["observations"])
        mine = sorted((o["artifact_id"], o["byte_start"], o["byte_end"], o["token_digest"]) for o in observations)
        if theirs != mine:
            raise Reject("witness observations differ from the facts recorded for this subject")
    entry["accepted"] = True
    entry["detail"] = "verdict and reason re-derived from verified token bytes"


def _rederive(claim, predicate, arts, records, files):
    args = claim["arguments"]
    if not isinstance(args, dict):
        return "INVALID_EVIDENCE", "malformed_claim_arguments", []
    ap = args.get("applicability")
    if ap is not None:
        if isinstance(ap, dict) and ap.get("decision") == "NOT_APPLICABLE" and all(
                isinstance(ap.get(k), str) and ap[k].strip() for k in ("rationale", "scope")):
            return "NOT_APPLICABLE", "reviewer_applicability_decision", []
        if not (isinstance(ap, dict) and ap.get("decision") == "APPLICABLE"):
            return "INVALID_EVIDENCE", "malformed_claim_arguments", []
    allowed = {"fact.equals": {"expected"}, "fact.numeric_bound": {"op", "bound"}, "fact.present": set()}[predicate]
    if set(args) - allowed - {"applicability"}:
        return "INVALID_EVIDENCE", "malformed_claim_arguments", []
    expected = args.get("expected")
    bound = args.get("bound")
    if predicate == "fact.equals" and not _typed(expected):
        return "INVALID_EVIDENCE", "malformed_claim_arguments", []
    if predicate == "fact.numeric_bound":
        if args.get("op") not in ("lt", "lte", "gt", "gte") or not (_typed(bound) and _numeric(bound)):
            return "INVALID_EVIDENCE", "malformed_claim_arguments", []
    prov_ref = next((r for r in claim["evidence_refs"] if r in arts and arts[r]["media_type"] == PROVENANCE_MEDIA), None)
    origin = _strict(files[arts[prov_ref]["relative_path"]]).get("origin_source_artifact_id") if prov_ref else None
    sources, obs, blocked, unusable, excluded = [], [], [], [], False
    for ref in claim["evidence_refs"]:
        if ref not in arts or arts[ref]["media_type"] == PROVENANCE_MEDIA:
            continue
        if ref == origin:
            excluded = True
            continue
        sources.append(ref)
        entry = records.get(ref)
        if entry is None:
            unusable.append((ref, "NO_EXTRACTION_RECORD"))
            continue
        rec, rend = entry
        summary = rec["coverage"]["summary"]
        if summary not in ("PARSED", "PARTIALLY_PARSED"):
            unusable.append((ref, summary))
            continue
        for fact in rec["facts"]:
            if fact["key"] == claim["subject"]:
                value = _verify_fact(files, arts, rend, fact)
                loc = fact["locator"]
                obs.append({"artifact_id": ref, "byte_start": loc["byte_start"], "byte_end": loc["byte_end"],
                            "token_digest": fact["token_digest"], "value": value})
        blocked += [b for b in rec.get("blocked_keys", []) if b["key"] == claim["subject"]]
    if not sources:
        return "INSUFFICIENT_EVIDENCE", "no_independent_evidence", []
    if blocked and not obs:
        return "HUMAN_REVIEW_REQUIRED", "evidence_key_ambiguous", []
    if not obs:
        only_corrupt = unusable and len(unusable) == len(sources) and all(c == "CORRUPT" for _, c in unusable)
        return ("INVALID_EVIDENCE", "evidence_unreadable", []) if only_corrupt \
            else ("INSUFFICIENT_EVIDENCE", "fact_not_found", [])
    if predicate == "fact.present":
        return "SUPPORTED", "fact_present", obs
    if predicate == "fact.equals":
        res = [_eq(o["value"], expected) for o in obs]
        if any(r is None for r in res):
            return "HUMAN_REVIEW_REQUIRED", "kind_mismatch", obs
        if any(r is False for r in res) and any(res):
            return "HUMAN_REVIEW_REQUIRED", "evidence_conflict", obs
        if any(r is False for r in res):
            return "CONTRADICTED", "value_mismatch", obs
        if unusable or blocked:
            return "HUMAN_REVIEW_REQUIRED", "evidence_source_unreadable", obs
        return "SUPPORTED", "value_equal", obs
    if not all(_numeric(o["value"]) for o in obs):
        return "HUMAN_REVIEW_REQUIRED", "kind_mismatch", obs
    b = Decimal(bound["value"])
    cmp = {"lt": lambda x: x < b, "lte": lambda x: x <= b, "gt": lambda x: x > b, "gte": lambda x: x >= b}[args["op"]]
    res = [cmp(Decimal(o["value"]["value"])) for o in obs]
    if all(res) and not (unusable or blocked):
        return "SUPPORTED", "bound_satisfied", obs
    if not any(res):
        return "CONTRADICTED", "bound_violated", obs
    return "HUMAN_REVIEW_REQUIRED", ("evidence_conflict" if not all(res) else "evidence_source_unreadable"), obs


def _typed(v) -> bool:
    if not (isinstance(v, dict) and set(v) == {"kind", "value"}):
        return False
    k, x = v["kind"], v["value"]
    try:
        if k in ("integer", "decimal"):
            Decimal(x)
            return isinstance(x, str)
    except (InvalidOperation, TypeError):
        return False
    return (k == "string" and isinstance(x, str)) or (k == "boolean" and isinstance(x, bool)) \
        or (k == "null" and x is None)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python -m verifier_lab.checker <package-dir-or-zip>", file=sys.stderr)
        return 64
    out = verify_package(Path(argv[1]))
    print(json.dumps(out, indent=2))
    return 0 if out["status"] == "VERIFIED" else 3


if __name__ == "__main__":
    sys.exit(main(sys.argv))

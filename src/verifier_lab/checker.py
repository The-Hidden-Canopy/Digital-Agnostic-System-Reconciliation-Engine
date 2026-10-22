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
               ("rule.interval_relation", "1"): "interval.relation",
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
    _check_obligations(result, bundle, files, arts, claims, rep)
    _check_purposes(result, bundle, files, arts, records, rep)
    missing = set(claims) - seen_claims
    rep.check("every_claim_has_a_finding", not missing, f"{len(missing)} claims without findings")
    rep.check("claim_index_complete", set(claim_index) == set(claims))


RISKPACK_MEDIA = "application/vnd.verifier-lab.riskpack+json"
CONTEXT_MEDIA = "application/vnd.verifier-lab.context-facts+json"
BUILTIN_PROFILE = "verifier-lab.builtin-rules"


def _t_and(a, b):
    if "FALSE" in (a, b):
        return "FALSE"
    return "TRUE" if a == b == "TRUE" else "UNKNOWN"


def _t_or(a, b):
    if "TRUE" in (a, b):
        return "TRUE"
    return "FALSE" if a == b == "FALSE" else "UNKNOWN"


def _pred(p, facts):
    """Own three-valued evaluator (no code shared with the producer)."""
    op = p["op"]
    if op in ("eq", "in", "exists"):
        k = p["fact"]
        if k not in facts:
            return "UNKNOWN", [k]
        v = facts[k]
        if op == "exists":
            return ("FALSE" if v is None else "TRUE"), [k]
        if v is None:
            return "FALSE", [k]
        cands = [p["value"]] if op == "eq" else p["values"]
        return ("TRUE" if any(type(v) is type(c) and v == c for c in cands) else "FALSE"), [k]
    if op == "not":
        r, u = _pred(p["arg"], facts)
        return {"TRUE": "FALSE", "FALSE": "TRUE", "UNKNOWN": "UNKNOWN"}[r], u
    acc, used = ("TRUE" if op == "and" else "FALSE"), []
    for a in p["args"]:
        r, u = _pred(a, facts)
        acc, used = (_t_and if op == "and" else _t_or)(acc, r), used + u
    return acc, sorted(set(used))


def _aggregate(vs):
    if "CONTRADICTED" in vs:
        return "CONTRADICTED"
    if vs and all(v == "SUPPORTED" for v in vs):
        return "SUPPORTED"
    for v in ("HUMAN_REVIEW_REQUIRED", "INVALID_EVIDENCE"):
        if v in vs:
            return v
    if vs and all(v == "NOT_APPLICABLE" for v in vs):
        return "NOT_APPLICABLE"
    return "INSUFFICIENT_EVIDENCE"


def _check_obligations(result, bundle, files, arts, claims, rep):
    purpose_keys = {(b.get("profile_id"), b.get("version")) for b in
                    (_strict(files[a["relative_path"]]) for a in arts.values() if a["media_type"] == "application/vnd.verifier-lab.purpose-profile+json")}
    pack_refs = [p for p in bundle["profile_refs"] if p["profile_id"] != BUILTIN_PROFILE
                 and (p["profile_id"], p["profile_version"]) not in purpose_keys]
    recorded = result.get("obligations")
    if not pack_refs:
        rep.check("no_risk_packs_no_obligations", not recorded and not result.get("risk_packs"),
                  "result carries obligations although no pack is pinned in the bundle")
        return
    ctx_art = next((a for a in arts.values() if a["media_type"] == CONTEXT_MEDIA), None)
    if not rep.check("context_artifact_present", ctx_art is not None):
        return
    facts = _strict(files[ctx_art["relative_path"]])
    rep.check("context_digest_pinned", result.get("context_digest") == _dg(_canon(facts)))
    findings = {f["claim_id"]: f["verdict"] for f in result["findings"]}
    cl = [{"id": c["id"], "predicate": c["predicate"], "subject": c["subject"]} for c in claims.values()]
    expected = []
    pack_arts = [_strict(files[a["relative_path"]]) for a in arts.values() if a["media_type"] == RISKPACK_MEDIA]
    for ref in sorted(pack_refs, key=lambda r: r["profile_id"]):
        body = next((b for b in pack_arts if b.get("pack_id") == ref["profile_id"] and b.get("version") == ref["profile_version"]), None)
        if not rep.check(f"pack_artifact:{ref['profile_id']}", body is not None, "pinned pack bytes missing from package"):
            continue
        digest = _dg(_canon({k: v for k, v in body.items() if k != "content_digest"}))
        if not rep.check(f"pack_digest_pinned:{ref['profile_id']}", digest == ref["digest"] == body.get("content_digest"),
                         "pack content differs from the digest pinned in the bundle"):
            continue
        pa, pu = _pred(body["applicability"], facts)
        for o in sorted(body["obligations"], key=lambda x: x["obligation_id"]):
            oa, ou = _pred(o["applicability"], facts) if "applicability" in o else ("TRUE", [])
            app = _t_and(pa, oa)
            used = sorted(set(pu + ou))
            e = {"pack_id": body["pack_id"], "obligation_id": o["obligation_id"],
                 "applicability": {"result": app, "facts_used": used, "facts": {k: facts[k] for k in used if k in facts}}}
            if app == "FALSE":
                e.update(verdict="NOT_APPLICABLE", reason="applicability_established_false", claim_ids=[], finding_ids=[])
            elif app == "UNKNOWN":
                human = o.get("classification_requires_human")
                e.update(verdict="HUMAN_REVIEW_REQUIRED" if human else "INSUFFICIENT_EVIDENCE",
                         reason="applicability_requires_human_classification" if human else "applicability_unknown",
                         claim_ids=[], finding_ids=[])
            else:
                ms = [c for c in cl if c["predicate"] == o["claim_predicate"] and ("subject" not in o or c["subject"] == o["subject"])]
                if not ms:
                    e.update(verdict="INSUFFICIENT_EVIDENCE", reason="no_claim_for_obligation", claim_ids=[], finding_ids=[])
                else:
                    fids = sorted(f["id"] for f in result["findings"] if f["claim_id"] in {c["id"] for c in ms})
                    e.update(verdict=_aggregate([findings[c["id"]] for c in ms if c["id"] in findings]),
                             reason="member_findings_aggregated", claim_ids=sorted(c["id"] for c in ms), finding_ids=fids)
            expected.append(e)
    got = {(x["pack_id"], x["obligation_id"]): x for x in (recorded or [])}
    rep.check("obligation_set_complete", set(got) == {(e["pack_id"], e["obligation_id"]) for e in expected},
              "obligations in result differ from the pinned packs")
    for e in expected:
        g = got.get((e["pack_id"], e["obligation_id"]))
        same = bool(g) and all(g.get(k) == e[k] for k in ("applicability", "verdict", "reason", "claim_ids", "finding_ids"))
        rep.check(f"obligation:{e['pack_id']}/{e['obligation_id']}", same,
                  f"re-derived {e['verdict']}/{e['reason']}; result says {g and (g.get('verdict'), g.get('reason'))}")


PURPOSE_MEDIA = "application/vnd.verifier-lab.purpose-profile+json"
PURPOSE_BINDINGS_MEDIA = "application/vnd.verifier-lab.purpose-bindings+json"
_KIND = {"any": lambda k: True, "number": lambda k: k in ("integer", "decimal"), "integer": lambda k: k == "integer",
         "decimal": lambda k: k == "decimal", "string": lambda k: k == "string", "boolean": lambda k: k == "boolean"}


def _check_purposes(result, bundle, files, arts, records, rep):
    pa = [a for a in arts.values() if a["media_type"] == PURPOSE_BINDINGS_MEDIA]
    recorded = result.get("purposes")
    if not pa:
        rep.check("no_purposes_no_assessments", not recorded, "result carries purpose assessments although none is pinned in the bundle")
        return
    profiles = [_strict(files[a["relative_path"]]) for a in arts.values() if a["media_type"] == PURPOSE_MEDIA]
    expected = []
    for a in sorted(pa, key=lambda x: x["id"]):
        b = _strict(files[a["relative_path"]])
        ref = b["profile"]
        body = next((p for p in profiles if p.get("profile_id") == ref["profile_id"] and p.get("version") == ref["profile_version"]), None)
        if not rep.check(f"purpose_profile_artifact:{ref['profile_id']}", body is not None, "pinned profile bytes missing"):
            continue
        d = _dg(_canon({k: v for k, v in body.items() if k != "content_digest"}))
        pinned = any(p["profile_id"] == ref["profile_id"] and p["profile_version"] == ref["profile_version"] and p["digest"] == d == body.get("content_digest")
                     for p in bundle["profile_refs"])
        if not rep.check(f"purpose_digest_pinned:{ref['profile_id']}", pinned, "profile content differs from the digest pinned in the bundle"):
            continue
        srcs = []
        for sid in b["evidence_sources"]:
            entry = records.get(sid)
            if entry is None:
                rep.check(f"purpose_evidence_extracted:{sid[-8:]}", False, "evidence source has no extraction record")
                continue
            rec, rend = entry
            srcs.append({"id": sid, "name": arts[sid]["relative_path"], "coverage": rec["coverage"]["summary"], "facts": rec["facts"],
                         "blocked": rec.get("blocked_keys", []), "rend": rend})
        try:
            expected.append((ref, _purpose(body, b["bindings"], srcs, files, arts)))
        except Reject as exc:
            rep.check(f"purpose_rederived:{ref['profile_id']}", False, str(exc))
    got = result.get("purposes") or []
    rep.check("purpose_set_complete", sorted((g["profile"]["profile_id"], g["profile"]["profile_version"]) for g in got)
              == sorted((r["profile_id"], r["profile_version"]) for r, _ in expected), "assessments differ from the pinned purposes")
    for ref, (overall, obs) in expected:
        g = next((x for x in got if x["profile"]["profile_id"] == ref["profile_id"]), None)
        mine = [(o[0], o[1], o[2]) for o in obs]
        theirs = [(o["id"], o["verdict"], o["reason"]) for o in (g or {}).get("obligations", [])]
        rep.check(f"purpose:{ref['profile_id']}", bool(g) and g["overall"] == overall and theirs == mine,
                  f"re-derived {overall} {mine}; result says {g and g['overall']} {theirs}")


def _purpose(profile, bindings, srcs, files, arts):
    """Independent evaluator: returns (overall, [(obligation id, verdict, reason)])."""
    usable = [s for s in srcs if s["coverage"] in ("PARSED", "PARTIALLY_PARSED")]
    unreadable = [s for s in srcs if s["coverage"] not in ("PARSED", "PARTIALLY_PARSED")]
    out, supplying = [], {}

    def verified(s, f):
        _verify_fact(files, arts, s["rend"], f)      # the cited token bytes must really say this
        return f
    for r in sorted(profile["roles"], key=lambda x: x["role"]):
        role, mn, mx, vk = r["role"], r.get("min", 1), r.get("max", 1), r.get("value_kind", "any")
        keys = bindings.get(role)
        if keys is None:
            if not r.get("optional"):
                out.append((f"role:{role}", "INSUFFICIENT_EVIDENCE", "role_not_bound"))
            continue
        found = [(s, verified(s, f)) for s in usable for f in s["facts"] if f["key"] in keys]
        blocked = [b for s in usable for b in s["blocked"] if b["key"] in keys]
        oid = f"role:{role}"
        if blocked:
            out.append((oid, "INVALID_EVIDENCE", "role_key_ambiguous"))
        elif any(not _KIND[vk](f["value"]["kind"]) for _, f in found):
            out.append((oid, "INVALID_EVIDENCE", "role_value_invalid"))
        elif len(found) < mn:
            out.append((oid, "INSUFFICIENT_EVIDENCE", "role_missing_unreadable_sources_present" if unreadable else "role_missing"))
        elif mx is not None and len(found) > mx:
            out.append((oid, "INVALID_EVIDENCE", "cardinality_exceeded"))
        else:
            out.append((oid, "SUPPORTED", "role_satisfied"))
            for s, _ in found:
                supplying[s["id"]] = s
    for c in profile.get("coherence", []):
        oid = f"coherence:{c['id']}"
        if not supplying:
            out.append((oid, "INSUFFICIENT_EVIDENCE", "not_executed_no_role_satisfied"))
            continue
        vals, missing = [], 0
        for s in supplying.values():
            hit = [f for f in s["facts"] if f["key"] == c["fact"]]
            if len(hit) == 1:
                vals.append(json.dumps(verified(s, hit[0])["value"], sort_keys=True))
            else:
                missing += 1
        out.append((oid, "INSUFFICIENT_EVIDENCE", "generation_unstated") if missing else
                   (oid, "CONTRADICTED", "sources_from_different_generations") if len(set(vals)) > 1 else (oid, "SUPPORTED", "sources_coherent"))
    lc = profile.get("lifecycle")
    if lc:
        out.extend(_lifecycle_check(lc, usable, files, arts))
    verdicts = [v for _, v, _ in out]
    overall = ("CONTRADICTED" if "CONTRADICTED" in verdicts else "INVALID_EVIDENCE" if "INVALID_EVIDENCE" in verdicts
               else "SUPPORTED" if verdicts and all(v == "SUPPORTED" for v in verdicts) else "INSUFFICIENT_EVIDENCE")
    return overall, out


def _lifecycle_check(lc, usable, files, arts):
    pat = re.compile(r"^" + re.escape(lc["history"]) + r"\[(\d+)\]\.(stage|seq|clock)$")
    events, bad = [], False
    for s in usable:
        idx = {}
        for f in s["facts"]:
            m = pat.match(f["key"])
            if m:
                idx.setdefault(int(m.group(1)), {})[m.group(2)] = _verify_fact_ret(files, arts, s, f)
        for i, p in idx.items():
            if set(p) == {"stage", "seq", "clock"} and p["seq"]["kind"] == "integer" and p["stage"]["kind"] == "string" and p["clock"]["kind"] == "string":
                events.append((p["stage"]["value"], int(p["seq"]["value"]), p["clock"]["value"]))
            else:
                bad = True
    if bad:
        return [("lifecycle:records", "INVALID_EVIDENCE", "malformed_lifecycle_record")]
    slots = {}
    for st, q, c in events:
        if (c, q) in slots and slots[(c, q)] != st:
            return [("lifecycle:records", "INVALID_EVIDENCE", "duplicate_lifecycle_entry")]
        slots[(c, q)] = st
    have = {e[0] for e in events}
    req, need, todo = lc.get("requires", {}), set(), [lc["target"]]
    while todo:
        n = todo.pop()
        for m in req.get(n, []):
            if m not in need:
                need.add(m)
                todo.append(m)
    if sorted(x for x in need | {lc["target"]} if x not in have):
        return [("lifecycle:prerequisites", "INSUFFICIENT_EVIDENCE", "prerequisite_missing")]
    out = [("lifecycle:prerequisites", "SUPPORTED", "prerequisites_recorded")]
    tgt = [e for e in events if e[0] == lc["target"]]
    inv = [e for e in events if e[0] in set(lc.get("invalidating", []))]
    if not inv:
        return out + [("lifecycle:current_validity", "SUPPORTED", "no_invalidation_recorded")]
    cross = any(t[2] != i[2] for t in tgt for i in inv)
    later = any(t[2] == i[2] and i[1] > t[1] for t in tgt for i in inv)
    all_earlier = all(t[2] == i[2] and i[1] < t[1] for t in tgt for i in inv)
    if later and not cross:
        return out + [("lifecycle:current_validity", "CONTRADICTED", "invalidated_after_success")]
    if cross or not all_earlier:
        return out + [("lifecycle:current_validity", "INSUFFICIENT_EVIDENCE", "ordering_ambiguous")]
    return out + [("lifecycle:current_validity", "SUPPORTED", "success_after_invalidation")]


def _verify_fact_ret(files, arts, s, f):
    return _verify_fact(files, arts, s["rend"], f)


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
    allowed = {"fact.equals": {"expected"}, "fact.numeric_bound": {"op", "bound"}, "fact.present": set(),
               "interval.relation": {"left", "right", "relation", "coord"}}[predicate]
    if set(args) - allowed - {"applicability"}:
        return "INVALID_EVIDENCE", "malformed_claim_arguments", []
    expected = args.get("expected")
    bound = args.get("bound")
    if predicate == "fact.equals" and not _typed(expected):
        return "INVALID_EVIDENCE", "malformed_claim_arguments", []
    if predicate == "fact.numeric_bound":
        if args.get("op") not in ("lt", "lte", "gt", "gte") or not (_typed(bound) and _numeric(bound)):
            return "INVALID_EVIDENCE", "malformed_claim_arguments", []
    wanted = {claim["subject"]}
    if predicate == "interval.relation":
        names = [args.get("left"), args.get("right")]
        if not all(isinstance(n, str) and n.strip() for n in names) or names[0] == names[1] \
                or args.get("relation") not in ("precedes", "no_overlap", "overlaps") \
                or not (isinstance(args.get("coord"), str) and args["coord"].strip()):
            return "INVALID_EVIDENCE", "malformed_claim_arguments", []
        wanted = {f"{n}.{x}" for n in names for x in _IV_SUFFIXES}
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
            if fact["key"] in wanted:
                value = _verify_fact(files, arts, rend, fact)
                loc = fact["locator"]
                obs.append({"key": fact["key"], "artifact_id": ref, "byte_start": loc["byte_start"], "byte_end": loc["byte_end"],
                            "token_digest": fact["token_digest"], "value": value})
        blocked += [b for b in rec.get("blocked_keys", []) if b["key"] in wanted]
    if not sources:
        return "INSUFFICIENT_EVIDENCE", "no_independent_evidence", []
    if predicate == "interval.relation":
        if not obs and not blocked and unusable and len(unusable) == len(sources) and all(c == "CORRUPT" for _, c in unusable):
            return "INVALID_EVIDENCE", "evidence_unreadable", []
        return _rederive_interval(args, obs, unusable, blocked)
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


_IV_SUFFIXES = ("start", "end", "start_min", "start_max", "end_min", "end_max")


def _rederive_interval(args, obs, unusable, blocked):
    if blocked:
        return "HUMAN_REVIEW_REQUIRED", "evidence_key_ambiguous", obs
    by = {}
    for o in obs:
        by.setdefault(o["key"], []).append(o)

    def bound(name, which):
        pt, lo_k, hi_k = f"{name}.{which}", f"{name}.{which}_min", f"{name}.{which}_max"
        vals = {}
        for k in (pt, lo_k, hi_k):
            group = by.get(k, [])
            if len({json.dumps(o["value"], sort_keys=True) for o in group}) > 1:
                return "evidence_conflict"
            if group:
                if not _numeric(group[0]["value"]):
                    return "kind_mismatch"
                vals[k] = Decimal(group[0]["value"]["value"])
        lo, hi = vals.get(lo_k, vals.get(pt)), vals.get(hi_k, vals.get(pt))
        if lo is None or hi is None:
            return "bound_missing"
        if lo > hi or (pt in vals and not lo <= vals[pt] <= hi):
            return "bounds_inconsistent"
        return lo, hi

    b = {}
    for n in (args["left"], args["right"]):
        for w in ("start", "end"):
            r = bound(n, w)
            if isinstance(r, str):
                verdict = ("INSUFFICIENT_EVIDENCE" if r == "bound_missing"
                           else "INVALID_EVIDENCE" if r == "bounds_inconsistent" else "HUMAN_REVIEW_REQUIRED")
                return verdict, r, obs
            b[(n, w)] = r
    L, R = args["left"], args["right"]
    if any(b[(n, "end")][1] < b[(n, "start")][0] for n in (L, R)):
        return "INVALID_EVIDENCE", "bounds_inconsistent", obs
    # independent formulation: decide by enumerating the extreme corner cases instead of min/max shortcuts
    def always(pred):
        return all(pred(sl, el, sr, er) for sl in b[(L, "start")] for el in b[(L, "end")]
                   for sr in b[(R, "start")] for er in b[(R, "end")])

    def never(pred):
        return not any(pred(sl, el, sr, er) for sl in b[(L, "start")] for el in b[(L, "end")]
                       for sr in b[(R, "start")] for er in b[(R, "end")])
    overlap = lambda sl, el, sr, er: max(sl, sr) < min(el, er)
    rel = args["relation"]
    if rel == "precedes":
        pred = lambda sl, el, sr, er: el <= sr
        yes, no = always(pred), never(pred)
        if not yes and not no:      # corners can miss interior cases only for non-monotone predicates; these are monotone
            return "INSUFFICIENT_EVIDENCE", "relation_uncertain", obs
    else:
        yes, no = always(overlap), never(overlap)
        if rel == "no_overlap":
            yes, no = no, yes
        if not yes and not no:
            return "INSUFFICIENT_EVIDENCE", "relation_uncertain", obs
    if yes:
        return (("HUMAN_REVIEW_REQUIRED", "evidence_source_unreadable", obs) if unusable
                else ("SUPPORTED", "relation_holds", obs))
    return "CONTRADICTED", "relation_violated", obs


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

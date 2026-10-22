"""Generic risk-pack engine (chapter 11). Packs are DATA ONLY: a bounded predicate grammar, no eval, no code.

A pack maps stated context to verification obligations. A document mentioning a risk does not make it apply,
and an applicable obligation without evidence is never satisfied by the absence of a violation. Findings are
evidence assessments under declared rules, not certification of any kind.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import StrEnum
from importlib import resources
from pathlib import Path

from .canonical import CanonicalError, canonical_bytes, parse_digest, sha256_digest, strict_loads

PACK_SCHEMA = "verifier-lab/riskpack/1"
SUPPORTED_BUNDLE_VERSIONS = ("1",)
MAX_PREDICATE_DEPTH = 8
MAX_OBLIGATIONS = 200
ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._\-]{0,63}\Z")
OPS = ("eq", "in", "exists", "and", "or", "not")
PACK_FIELDS = {"schema", "pack_id", "version", "title", "description", "compatible_bundle_versions", "applicability",
               "obligations", "source_refs", "requires", "content_digest"}
OBLIGATION_FIELDS = {"obligation_id", "title", "claim_predicate", "subject", "rule_ref", "applicability",
                     "evidence_requirements", "review_requirements", "classification_requires_human"}
REQ_FIELDS = {"id", "description"}
SOURCE_FIELDS = {"title", "edition", "note", "uri"}


class PackError(ValueError):
    def __init__(self, code: str, message: str = ""):
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code


class Tri(StrEnum):
    TRUE = "TRUE"
    FALSE = "FALSE"
    UNKNOWN = "UNKNOWN"


def tri_and(a: Tri, b: Tri) -> Tri:
    if Tri.FALSE in (a, b):
        return Tri.FALSE
    return Tri.TRUE if (a, b) == (Tri.TRUE, Tri.TRUE) else Tri.UNKNOWN


def tri_or(a: Tri, b: Tri) -> Tri:
    if Tri.TRUE in (a, b):
        return Tri.TRUE
    return Tri.FALSE if (a, b) == (Tri.FALSE, Tri.FALSE) else Tri.UNKNOWN


def tri_not(a: Tri) -> Tri:
    return {Tri.TRUE: Tri.FALSE, Tri.FALSE: Tri.TRUE, Tri.UNKNOWN: Tri.UNKNOWN}[a]


def _scalar(v) -> bool:
    return v is None or isinstance(v, (str, bool)) or (isinstance(v, int) and not isinstance(v, bool))


def validate_predicate(p, depth: int = 0) -> None:
    if depth > MAX_PREDICATE_DEPTH:
        raise PackError("predicate_too_deep")
    if not isinstance(p, dict) or p.get("op") not in OPS:
        raise PackError("bad_predicate", f"op must be one of {OPS}")
    op = p["op"]
    allowed = {"eq": {"op", "fact", "value"}, "in": {"op", "fact", "values"}, "exists": {"op", "fact"},
               "and": {"op", "args"}, "or": {"op", "args"}, "not": {"op", "arg"}}[op]
    if set(p) - allowed:
        raise PackError("unknown_predicate_field", str(sorted(set(p) - allowed)))
    if op in ("eq", "in", "exists"):
        if not isinstance(p.get("fact"), str) or not p["fact"]:
            raise PackError("bad_predicate", "fact must be a non-empty string")
    if op == "eq" and (("value" not in p) or not _scalar(p["value"]) or p["value"] is None):
        raise PackError("bad_predicate", "eq needs a non-null scalar value")
    if op == "in":
        vals = p.get("values")
        if not isinstance(vals, list) or not vals or not all(_scalar(v) and v is not None for v in vals):
            raise PackError("bad_predicate", "in needs a non-empty finite list of scalars")
    if op in ("and", "or"):
        if not isinstance(p.get("args"), list) or not p["args"]:
            raise PackError("bad_predicate", "args must be a non-empty list")
        for a in p["args"]:
            validate_predicate(a, depth + 1)
    if op == "not":
        validate_predicate(p.get("arg"), depth + 1)


def evaluate_predicate(p: dict, facts: dict) -> tuple[Tri, list[str]]:
    """Three-valued; returns (result, facts consulted). Missing facts are UNKNOWN, never FALSE."""
    op = p["op"]
    if op in ("eq", "in", "exists"):
        key = p["fact"]
        if key not in facts:
            return Tri.UNKNOWN, [key]
        v = facts[key]
        if op == "exists":
            return (Tri.FALSE if v is None else Tri.TRUE), [key]       # null = explicitly stated absent
        if v is None:
            return Tri.FALSE, [key]
        want = [p["value"]] if op == "eq" else p["values"]
        same = lambda a, b: type(a) is type(b) and a == b              # True != 1: no coercion
        return (Tri.TRUE if any(same(v, w) for w in want) else Tri.FALSE), [key]
    if op == "not":
        r, used = evaluate_predicate(p["arg"], facts)
        return tri_not(r), used
    fold = tri_and if op == "and" else tri_or
    acc, used = (Tri.TRUE if op == "and" else Tri.FALSE), []
    for a in p["args"]:
        r, u = evaluate_predicate(a, facts)
        acc, used = fold(acc, r), used + u
    return acc, sorted(set(used))


@dataclass(frozen=True)
class RiskPack:
    pack_id: str
    version: str
    content_digest: str
    body: dict                     # canonical, validated, immutable by convention

    @property
    def obligations(self) -> list[dict]:
        return self.body["obligations"]

    def profile_ref(self) -> dict:
        return {"profile_id": self.pack_id, "profile_version": self.version, "digest": self.content_digest}


def content_digest(body: dict) -> str:
    return sha256_digest(canonical_bytes({k: v for k, v in body.items() if k != "content_digest"}))


def seal(body: dict) -> dict:
    """Stamp the content digest (used when authoring a pack file)."""
    out = {k: v for k, v in body.items() if k != "content_digest"}
    return {**out, "content_digest": content_digest(out)}


def load_pack(data: bytes, known_rules: dict[tuple[str, str], str]) -> RiskPack:
    """Parse and validate. `known_rules` maps (rule_id, version) -> predicate it implements."""
    try:
        body = strict_loads(data, max_bytes=2_000_000, max_depth=16)
    except CanonicalError as exc:
        raise PackError("malformed_pack", str(exc)) from exc
    if not isinstance(body, dict):
        raise PackError("malformed_pack", "not an object")
    extra = set(body) - PACK_FIELDS
    if extra:
        raise PackError("unknown_field", f"{sorted(extra)} (packs are data only; no code or unknown fields)")
    if body.get("schema") != PACK_SCHEMA:
        raise PackError("unsupported_schema")
    for k in ("pack_id", "version", "title", "compatible_bundle_versions", "applicability", "obligations",
              "source_refs", "content_digest"):
        if k not in body:
            raise PackError("missing_field", k)
    if not ID_RE.match(str(body["pack_id"])) or not isinstance(body["version"], str) or not body["version"]:
        raise PackError("bad_identity")
    if not parse_digest(body["content_digest"]) or body["content_digest"] != content_digest(body):
        raise PackError("digest_mismatch", "content_digest does not match pack content")
    cbv = body["compatible_bundle_versions"]
    if not isinstance(cbv, list) or not cbv or not all(isinstance(x, str) for x in cbv):
        raise PackError("bad_compatibility")
    if not set(cbv) & set(SUPPORTED_BUNDLE_VERSIONS):
        raise PackError("unsupported_bundle_version", str(cbv))
    validate_predicate(body["applicability"])
    obs = body["obligations"]
    if not isinstance(obs, list) or not obs or len(obs) > MAX_OBLIGATIONS:
        raise PackError("bad_obligations")
    seen = set()
    for o in obs:
        if not isinstance(o, dict):
            raise PackError("bad_obligation")
        if set(o) - OBLIGATION_FIELDS:
            raise PackError("unknown_field", f"obligation {sorted(set(o) - OBLIGATION_FIELDS)}")
        for k in ("obligation_id", "claim_predicate", "rule_ref"):
            if k not in o:
                raise PackError("missing_field", f"obligation.{k}")
        if not ID_RE.match(str(o["obligation_id"])):
            raise PackError("bad_identity", "obligation id")
        if o["obligation_id"] in seen:
            raise PackError("duplicate_obligation_id", o["obligation_id"])
        seen.add(o["obligation_id"])
        rr = o["rule_ref"]
        if not (isinstance(rr, dict) and set(rr) == {"rule_id", "version"}):
            raise PackError("bad_rule_ref")
        implemented = known_rules.get((rr["rule_id"], rr["version"]))
        if implemented is None:
            raise PackError("unknown_rule", f"{rr['rule_id']}@{rr['version']} is not in the installed rule catalog")
        if implemented != o["claim_predicate"]:
            raise PackError("rule_predicate_mismatch", f"{rr['rule_id']} implements {implemented}")
        if "applicability" in o:
            validate_predicate(o["applicability"])
        if "subject" in o and not (isinstance(o["subject"], str) and o["subject"]):
            raise PackError("bad_subject")
        for r in o.get("evidence_requirements", []):
            if not isinstance(r, dict) or set(r) - REQ_FIELDS or "id" not in r:
                raise PackError("bad_evidence_requirement")
        if not all(isinstance(x, str) for x in o.get("review_requirements", [])):
            raise PackError("bad_review_requirements")
        if "classification_requires_human" in o and not isinstance(o["classification_requires_human"], bool):
            raise PackError("bad_flag")
    for s in body["source_refs"]:
        if not isinstance(s, dict) or set(s) - SOURCE_FIELDS or "title" not in s:
            raise PackError("bad_source_ref")
    req = body.get("requires", [])
    if not isinstance(req, list) or not all(isinstance(x, str) for x in req):
        raise PackError("bad_requires")
    return RiskPack(body["pack_id"], body["version"], body["content_digest"], body)


def check_facts(facts) -> dict:
    if not isinstance(facts, dict) or len(facts) > 500:
        raise PackError("bad_context", "context must be an object of at most 500 facts")
    for k, v in facts.items():
        if not isinstance(k, str) or not k or not _scalar(v):
            raise PackError("bad_context", f"fact {k!r} must be a string, integer, boolean or null")
    return dict(sorted(facts.items()))


def context_digest(facts: dict) -> str:
    return sha256_digest(canonical_bytes(check_facts(facts)))


class PackCatalog:
    """Installed packs: built-in synthetic examples plus a local directory. Identity is (pack_id, version)."""

    def __init__(self, directory: Path | None, known_rules: dict[tuple[str, str], str]):
        self.known_rules = known_rules
        self.directory = directory
        self._packs: dict[tuple[str, str], RiskPack] = {}
        self.skipped: list[dict] = []
        for name in resources.files("verifier_lab").joinpath("riskpacks_data").iterdir():
            if name.name.endswith(".json"):
                try:
                    self._add(load_pack(name.read_bytes(), known_rules), builtin=True)
                except PackError as exc:       # e.g. a restricted rule catalog: the pack is unavailable, not half-loaded
                    self.skipped.append({"file": name.name, "code": exc.code})
        if directory and directory.exists():
            for p in sorted(directory.glob("*.json")):
                try:
                    self._add(load_pack(p.read_bytes(), known_rules))
                except PackError:
                    continue            # an invalid installed file is ignored, never half-loaded

    def _add(self, pack: RiskPack, builtin: bool = False) -> None:
        key = (pack.pack_id, pack.version)
        old = self._packs.get(key)
        if old and old.content_digest != pack.content_digest:
            raise PackError("conflicting_content", f"{key[0]}@{key[1]} already installed with different content")
        self._packs[key] = pack

    def install(self, data: bytes) -> RiskPack:
        pack = load_pack(data, self.known_rules)
        self._add(pack)
        if self.directory:
            self.directory.mkdir(parents=True, exist_ok=True)
            target = self.directory / f"{pack.pack_id}@{pack.version}.json"
            if not target.exists():
                target.write_bytes(canonical_bytes(pack.body))
        return pack

    def list(self) -> list[RiskPack]:
        return [self._packs[k] for k in sorted(self._packs)]

    def get(self, pack_id: str, version: str | None = None) -> RiskPack:
        cands = [p for (i, v), p in self._packs.items() if i == pack_id and version in (None, v)]
        if not cands:
            raise PackError("pack_not_found", pack_id)
        return sorted(cands, key=lambda p: p.version)[-1]

    def resolve(self, pack_ids: list[str]) -> list[RiskPack]:
        """Selected packs plus transitive `requires`; cycles are rejected; order is stable."""
        out: dict[str, RiskPack] = {}

        def visit(pid: str, stack: tuple[str, ...]):
            if pid in stack:
                raise PackError("cyclic_pack_dependency", " -> ".join(stack + (pid,)))
            if pid in out:
                return
            pack = self.get(pid)
            for r in pack.body.get("requires", []):
                visit(r, stack + (pid,))
            out[pid] = pack
        for pid in sorted(set(pack_ids)):
            visit(pid, ())
        return [out[k] for k in sorted(out)]


def diff_packs(old: RiskPack, new: RiskPack) -> dict:
    """Machine-readable change summary; a version label never replaces this diff."""
    o = {x["obligation_id"]: x for x in old.obligations}
    n = {x["obligation_id"]: x for x in new.obligations}
    changed = []
    for oid in sorted(set(o) & set(n)):
        delta = [k for k in sorted(OBLIGATION_FIELDS) if o[oid].get(k) != n[oid].get(k)]
        if delta:
            changed.append({"obligation_id": oid, "fields": delta})
    return {"from": old.profile_ref(), "to": new.profile_ref(),
            "added_obligations": sorted(set(n) - set(o)), "removed_obligations": sorted(set(o) - set(n)),
            "changed_obligations": changed,
            "applicability_changed": old.body["applicability"] != new.body["applicability"],
            "source_refs_changed": old.body["source_refs"] != new.body["source_refs"],
            "requires_changed": old.body.get("requires", []) != new.body.get("requires", []),
            "identical": old.content_digest == new.content_digest}


# ------------------------------------------------------------------------------------------ planning
REMEDIATION = {
    "applicability_unknown": "Supply the missing context facts ({detail}); then re-run. Applicability is not assumed.",
    "no_claim_for_obligation": "Review and approve a claim of predicate '{predicate}' for subject '{subject}' from your documents, then re-run.",
    "no_independent_evidence": "Add an evidence source that states this fact independently of the document the claim came from.",
    "fact_not_found": "Provide evidence that states the fact; absence is not treated as satisfaction.",
    "value_mismatch": "Correct the source or the claim so they agree, or document why they legitimately differ; then re-run.",
    "evidence_conflict": "Resolve the conflict between evidence sources and record the decision; the sources are kept as they are.",
    "human_classification": "A person must classify this ({detail}); record the decision and scope.",
}


def _claim_matches(obligation: dict, claims: list[dict]) -> list[dict]:
    return [c for c in claims if c["predicate"] == obligation["claim_predicate"]
            and ("subject" not in obligation or c["subject"] == obligation["subject"])]


def aggregate(verdicts: list[str]) -> str:
    """Conjunction over essential claims: all SUPPORTED => SUPPORTED; one valid contradiction contradicts;
    unresolved branches stay listed (the caller keeps the member findings)."""
    if "CONTRADICTED" in verdicts:
        return "CONTRADICTED"
    if verdicts and all(v == "SUPPORTED" for v in verdicts):
        return "SUPPORTED"
    for v in ("HUMAN_REVIEW_REQUIRED", "INVALID_EVIDENCE"):
        if v in verdicts:
            return v
    if verdicts and all(v == "NOT_APPLICABLE" for v in verdicts):
        return "NOT_APPLICABLE"
    return "INSUFFICIENT_EVIDENCE"


def plan_and_assess(pack: RiskPack, facts: dict, claims: list[dict], findings_by_claim: dict[str, dict]) -> list[dict]:
    """claims: [{id, predicate, subject}]. findings_by_claim: claim id -> finding dict. Returns one entry per obligation."""
    pack_app, pack_used = evaluate_predicate(pack.body["applicability"], facts)
    out = []
    for o in sorted(pack.obligations, key=lambda x: x["obligation_id"]):
        entry = {"pack_id": pack.pack_id, "pack_version": pack.version, "pack_digest": pack.content_digest,
                 "obligation_id": o["obligation_id"], "claim_ids": [], "finding_ids": [], "remediation": []}
        own = evaluate_predicate(o["applicability"], facts) if "applicability" in o else (Tri.TRUE, [])
        app = tri_and(pack_app, own[0])
        used = sorted(set(pack_used + own[1]))
        entry["applicability"] = {"result": app.value, "facts_used": used,
                                  "facts": {k: facts[k] for k in used if k in facts}}
        if app == Tri.FALSE:
            entry.update(verdict="NOT_APPLICABLE", reason="applicability_established_false")
        elif app == Tri.UNKNOWN:
            missing = [k for k in used if k not in facts]
            if o.get("classification_requires_human"):
                entry.update(verdict="HUMAN_REVIEW_REQUIRED", reason="applicability_requires_human_classification")
                entry["remediation"].append(REMEDIATION["human_classification"].format(detail=", ".join(used) or "applicability"))
            else:
                entry.update(verdict="INSUFFICIENT_EVIDENCE", reason="applicability_unknown")
                entry["remediation"].append(REMEDIATION["applicability_unknown"].format(detail=", ".join(missing) or "see context"))
        else:
            matches = _claim_matches(o, claims)
            if not matches:
                entry.update(verdict="INSUFFICIENT_EVIDENCE", reason="no_claim_for_obligation")
                entry["remediation"].append(REMEDIATION["no_claim_for_obligation"].format(
                    predicate=o["claim_predicate"], subject=o.get("subject", "(any)")))
            else:
                fs = [findings_by_claim[c["id"]] for c in matches if c["id"] in findings_by_claim]
                entry["claim_ids"] = sorted(c["id"] for c in matches)
                entry["finding_ids"] = sorted(f["id"] for f in fs)
                verdict = aggregate([f["verdict"] for f in fs])
                entry.update(verdict=verdict, reason="member_findings_aggregated",
                             member_verdicts={f["id"]: f["verdict"] for f in fs})
                if o.get("classification_requires_human") and verdict == "SUPPORTED":
                    entry["review_requirements"] = o.get("review_requirements", [])
                for f in fs:
                    tpl = REMEDIATION.get(f["reason_code"])
                    if tpl and f["verdict"] != "SUPPORTED":
                        entry["remediation"].append(tpl.format(predicate=o["claim_predicate"], subject=o.get("subject", ""), detail=""))
        if o.get("evidence_requirements"):
            entry["evidence_requirements"] = [r["id"] for r in o["evidence_requirements"]]
        out.append(entry)
    return out

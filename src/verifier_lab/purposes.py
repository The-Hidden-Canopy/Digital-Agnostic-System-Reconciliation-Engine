"""Purpose-relative completeness (chapter 04): a package is sufficient *for a stated purpose*, never simply complete.

Profiles are data-only (no code, no dynamic imports). Evidence roles are bound to fact keys by explicit references - never
inferred from file names. Each obligation keeps its own verdict; no overall SUPPORTED exists while an essential obligation
is unmet or unexecuted.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from . import riskpacks as RP
from .canonical import CanonicalError, canonical_bytes, parse_digest, sha256_digest, strict_loads

SCHEMA = "verifier-lab/purpose/1"
PROFILE_FIELDS = {"schema", "profile_id", "version", "title", "description", "roles", "coherence", "lifecycle", "applies_when", "content_digest"}
ROLE_FIELDS = {"role", "description", "min", "max", "value_kind", "optional"}
COHERENCE_FIELDS = {"id", "type", "fact"}
LIFECYCLE_FIELDS = {"target", "history", "stages", "requires", "invalidating"}
VALUE_KINDS = ("any", "number", "integer", "decimal", "string", "boolean")
ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._\-]{0,63}\Z")
MAX_ROLES = 100


class PurposeError(ValueError):
    def __init__(self, code: str, message: str = ""):
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code


@dataclass(frozen=True)
class PurposeProfile:
    profile_id: str
    version: str
    content_digest: str
    body: dict

    def ref(self) -> dict:
        return {"profile_id": self.profile_id, "profile_version": self.version, "digest": self.content_digest}

    @property
    def roles(self) -> list[dict]:
        return self.body["roles"]


def content_digest(body: dict) -> str:
    return sha256_digest(canonical_bytes({k: v for k, v in body.items() if k != "content_digest"}))


def seal(body: dict) -> dict:
    b = {k: v for k, v in body.items() if k != "content_digest"}
    return {**b, "content_digest": content_digest(b)}


def _acyclic(requires: dict) -> bool:
    state: dict[str, int] = {}

    def visit(n):
        if state.get(n) == 1:
            return False
        if state.get(n) == 2:
            return True
        state[n] = 1
        ok = all(visit(m) for m in requires.get(n, []))
        state[n] = 2
        return ok
    return all(visit(n) for n in requires)


def load_profile(data: bytes) -> PurposeProfile:
    try:
        body = strict_loads(data, max_bytes=1_000_000, max_depth=12)
    except CanonicalError as exc:
        raise PurposeError("malformed_profile", str(exc)) from exc
    if not isinstance(body, dict):
        raise PurposeError("malformed_profile", "not an object")
    if set(body) - PROFILE_FIELDS:
        raise PurposeError("unknown_field", f"{sorted(set(body) - PROFILE_FIELDS)} (profiles are data only)")
    if body.get("schema") != SCHEMA:
        raise PurposeError("unsupported_schema")
    for k in ("profile_id", "version", "title", "roles", "content_digest"):
        if k not in body:
            raise PurposeError("missing_field", k)
    if not ID_RE.match(str(body["profile_id"])) or not isinstance(body["version"], str) or not body["version"]:
        raise PurposeError("bad_identity")
    if not parse_digest(body["content_digest"]) or body["content_digest"] != content_digest(body):
        raise PurposeError("digest_mismatch", "content_digest does not match profile content")
    roles = body["roles"]
    if not isinstance(roles, list) or not roles or len(roles) > MAX_ROLES:
        raise PurposeError("bad_roles")
    seen = set()
    for r in roles:
        if not isinstance(r, dict) or set(r) - ROLE_FIELDS or "role" not in r:
            raise PurposeError("bad_role", "unknown or missing role fields")
        if not ID_RE.match(str(r["role"])) or r["role"] in seen:
            raise PurposeError("bad_role", f"duplicate or malformed role id {r.get('role')!r}")
        seen.add(r["role"])
        mn, mx = r.get("min", 1), r.get("max", 1)
        if not isinstance(mn, int) or isinstance(mn, bool) or mn < 0 or (mx is not None and (not isinstance(mx, int) or isinstance(mx, bool) or mx < 1)):
            raise PurposeError("bad_cardinality", r["role"])
        if mx is not None and mn > mx:
            raise PurposeError("invalid_profile_cardinality", f"role {r['role']}: min {mn} > max {mx}")
        if r.get("optional") and mn != 0:
            raise PurposeError("bad_cardinality", f"optional role {r['role']} must have min 0")
        if r.get("value_kind", "any") not in VALUE_KINDS:
            raise PurposeError("bad_value_kind", r["role"])
    if "applies_when" in body:
        try:
            RP.validate_predicate(body["applies_when"])
        except RP.PackError as exc:
            raise PurposeError("bad_applicability", str(exc)) from exc
    coh_ids = set()
    for c in body.get("coherence", []):
        if not isinstance(c, dict) or set(c) != COHERENCE_FIELDS or c["type"] != "same_value" or not isinstance(c["fact"], str) or not c["fact"]:
            raise PurposeError("bad_coherence_rule", "only {id, type: same_value, fact} is supported")
        if c["id"] in coh_ids:
            raise PurposeError("duplicate_coherence_id", c["id"])
        coh_ids.add(c["id"])
    lc = body.get("lifecycle")
    if lc is not None:
        if not isinstance(lc, dict) or set(lc) - LIFECYCLE_FIELDS or not {"target", "history", "stages"} <= set(lc):
            raise PurposeError("bad_lifecycle")
        stages = lc["stages"]
        if not isinstance(stages, list) or len(set(stages)) != len(stages) or lc["target"] not in stages:
            raise PurposeError("bad_lifecycle", "stages must be unique and contain the target")
        req = lc.get("requires", {})
        if not isinstance(req, dict) or any(k not in stages or not set(v) <= set(stages) for k, v in req.items()):
            raise PurposeError("bad_lifecycle", "requires references an unknown stage")
        if not _acyclic(req):
            raise PurposeError("cyclic_lifecycle", "version one lifecycle graphs must be acyclic")
        if not set(lc.get("invalidating", [])) <= set(stages) | set(lc.get("invalidating", [])):
            raise PurposeError("bad_lifecycle")
    return PurposeProfile(body["profile_id"], body["version"], body["content_digest"], body)


class PurposeCatalog:
    def __init__(self, directory: Path | None):
        self.directory = directory
        self._p: dict[tuple[str, str], PurposeProfile] = {}
        for f in resources.files("verifier_lab").joinpath("purposes_data").iterdir():
            if f.name.endswith(".json"):
                self._add(load_profile(f.read_bytes()))
        if directory and directory.exists():
            for f in sorted(directory.glob("*.json")):
                try:
                    self._add(load_profile(f.read_bytes()))
                except PurposeError:
                    continue

    def _add(self, p: PurposeProfile) -> None:
        old = self._p.get((p.profile_id, p.version))
        if old and old.content_digest != p.content_digest:
            raise PurposeError("conflicting_content", f"{p.profile_id}@{p.version} is immutable; publish a new version or id")
        self._p[(p.profile_id, p.version)] = p

    def install(self, data: bytes) -> PurposeProfile:
        p = load_profile(data)
        self._add(p)
        if self.directory:
            self.directory.mkdir(parents=True, exist_ok=True)
            t = self.directory / f"{p.profile_id}@{p.version}.json"
            if not t.exists():
                t.write_bytes(canonical_bytes(p.body))
        return p

    def list(self) -> list[PurposeProfile]:
        return [self._p[k] for k in sorted(self._p)]

    def get(self, profile_id: str, version: str | None = None) -> PurposeProfile:
        c = [p for (i, v), p in self._p.items() if i == profile_id and version in (None, v)]
        if not c:
            raise PurposeError("profile_not_found", f"{profile_id}@{version or '*'}")
        return sorted(c, key=lambda p: p.version)[-1]

    def newer_than(self, profile_id: str, version: str) -> str | None:
        latest = self.get(profile_id).version
        return latest if latest != version else None


def strengthens(strong: PurposeProfile, weak: PurposeProfile) -> tuple[bool, list[str]]:
    """Mechanical relation only (never by name): `strong` requires at least everything `weak` requires."""
    if "applies_when" in strong.body or "applies_when" in weak.body:
        return False, ["applicability_conditions_not_comparable"]
    why = []
    sr = {r["role"]: r for r in strong.roles}
    for r in weak.roles:
        s = sr.get(r["role"])
        if s is None:
            if r.get("min", 1) > 0:
                why.append(f"role {r['role']} is required by the weaker profile but absent from the stronger one")
            continue
        if s.get("min", 1) < r.get("min", 1):
            why.append(f"role {r['role']}: minimum weakened")
        if s.get("max", 1) is not None and (r.get("max", 1) is None or s.get("max", 1) > r.get("max", 1)):
            why.append(f"role {r['role']}: maximum loosened")
        if s.get("value_kind", "any") != r.get("value_kind", "any") and s.get("value_kind", "any") == "any":
            why.append(f"role {r['role']}: value kind constraint dropped")
    sc = {c["id"] for c in strong.body.get("coherence", [])}
    why += [f"coherence rule {c['id']} dropped" for c in weak.body.get("coherence", []) if c["id"] not in sc]
    if weak.body.get("lifecycle") and not strong.body.get("lifecycle"):
        why.append("lifecycle requirement dropped")
    return not why, why


# --------------------------------------------------------------------------------------- evaluation
KIND_OK = {"any": lambda k: True, "number": lambda k: k in ("integer", "decimal"), "integer": lambda k: k == "integer",
           "decimal": lambda k: k == "decimal", "string": lambda k: k == "string", "boolean": lambda k: k == "boolean"}
STRONG_ORDER = ("CONTRADICTED", "INVALID_EVIDENCE", "INSUFFICIENT_EVIDENCE", "HUMAN_REVIEW_REQUIRED")


def _obs(src, fact) -> dict:
    loc = fact["locator"]
    return {"artifact_id": src["artifact_id"], "source": src["name"], "key": fact["key"], "value": fact["value"],
            "byte_start": loc["byte_start"], "byte_end": loc["byte_end"], "token_digest": fact["token_digest"],
            "grammar": fact["grammar"], "rendition_artifact_id": src.get("rendition_artifact_id")}


def check_bindings(profile: PurposeProfile, bindings: dict) -> dict:
    roles = {r["role"] for r in profile.roles}
    if not isinstance(bindings, dict):
        raise PurposeError("bad_bindings", "bindings must be an object role -> list of fact keys")
    out = {}
    for role, keys in bindings.items():
        if role not in roles:
            raise PurposeError("unknown_role", role)
        if not isinstance(keys, list) or not keys or not all(isinstance(k, str) and k for k in keys) or len(set(keys)) != len(keys):
            raise PurposeError("bad_bindings", f"role {role} needs a non-empty list of distinct fact keys")
        out[role] = sorted(keys)
    return dict(sorted(out.items()))


def evaluate_purpose(profile: PurposeProfile, bindings: dict, sources: list[dict], context: dict | None = None) -> dict:
    """sources: [{artifact_id, name, coverage, facts, blocked_keys}] - only independently supplied, extracted evidence.

    context: run context facts, consulted only by an optional `applies_when` condition (see docs/FORMATS.md)."""
    bindings = check_bindings(profile, bindings)
    obligations: list[dict] = []
    condition = profile.body.get("applies_when")
    if condition is not None:
        facts = context or {}
        tri, used = RP.evaluate_predicate(condition, facts)
        witness = {"predicate": condition, "facts": used, "values": {k: facts[k] for k in used if k in facts}}
        if tri is RP.Tri.FALSE:
            skipped = [f"role:{r['role']}" for r in sorted(profile.roles, key=lambda x: x["role"]) if not (r.get("optional") and r["role"] not in bindings)]
            skipped += [f"coherence:{c['id']}" for c in profile.body.get("coherence", [])]
            if profile.body.get("lifecycle"):
                skipped += ["lifecycle:records", "lifecycle:prerequisites", "lifecycle:current_validity"]
            return {"profile": profile.ref(), "bindings": bindings, "overall": "NOT_APPLICABLE",
                    "obligations": [{"id": "applicability", "kind": "applicability", "verdict": "NOT_APPLICABLE",
                                     "reason": "applicability_established_false", "witness": witness, "would_resolve": ""}],
                    "not_evaluated": skipped,
                    "scope": {"evaluated_sources": [], "unreadable_sources": [], "sources_outside_evaluation_scope": sorted(s["name"] for s in sources),
                              "statement": "The profile's applicability condition is false under the supplied context, so no evidence obligation was evaluated."},
                    "would_resolve": []}
        if tri is RP.Tri.TRUE:
            obligations.append({"id": "applicability", "kind": "applicability", "verdict": "SUPPORTED",
                                "reason": "applicability_established_true", "witness": witness, "would_resolve": ""})
        else:
            obligations.append({"id": "applicability", "kind": "applicability", "verdict": "INSUFFICIENT_EVIDENCE",
                                "reason": "applicability_unknown", "witness": witness,
                                "would_resolve": f"State the context fact(s) {', '.join(used)} (for example --context KEY=VALUE). A missing fact is never treated as false."})
    usable = [s for s in sources if s["coverage"] in ("PARSED", "PARTIALLY_PARSED")]
    unreadable = [s["name"] for s in sources if s["coverage"] not in ("PARSED", "PARTIALLY_PARSED")]
    supplying: dict[str, dict] = {}

    def ob(oid, kind, verdict, reason, witness=None, needs=None):
        obligations.append({"id": oid, "kind": kind, "verdict": verdict, "reason": reason, "witness": witness or {},
                            "would_resolve": needs or ""})

    # ---- role obligations, stable order
    for r in sorted(profile.roles, key=lambda x: x["role"]):
        role, mn, mx, vk = r["role"], r.get("min", 1), r.get("max", 1), r.get("value_kind", "any")
        keys = bindings.get(role)
        oid = f"role:{role}"
        if keys is None:
            if r.get("optional"):
                continue                                              # not supplied, not invented, not evaluated
            ob(oid, "role", "INSUFFICIENT_EVIDENCE", "role_not_bound", {"role": role},
               f"Bind role '{role}' to the fact key(s) that state it; roles are never inferred from names. Supplying it may or may not satisfy the purpose.")
            continue
        found, blocked = [], []
        for s in usable:
            for f in s["facts"]:
                if f["key"] in keys:
                    found.append((s, f))
            blocked += [(s, b) for b in s.get("blocked_keys", []) if b["key"] in keys]
        wit = {"role": role, "keys": keys, "observations": [_obs(s, f) for s, f in found]}
        if blocked:
            ob(oid, "role", "INVALID_EVIDENCE", "role_key_ambiguous", {**wit, "blocked": [b["key"] for _, b in blocked]},
               "Remove the duplicate definition so each role key is stated once.")
        elif any(not KIND_OK[vk](f["value"]["kind"]) for _, f in found):
            bad = [_obs(s, f) for s, f in found if not KIND_OK[vk](f["value"]["kind"])]
            ob(oid, "role", "INVALID_EVIDENCE", "role_value_invalid", {**wit, "expected_kind": vk, "invalid": bad},
               f"Supply the role as a {vk} value; a supplied record of the wrong type is invalid, not missing.")
        elif len(found) < mn:
            ob(oid, "role", "INSUFFICIENT_EVIDENCE" if not r.get("optional") else "INSUFFICIENT_EVIDENCE",
               "role_missing_unreadable_sources_present" if unreadable else "role_missing", {**wit, "found": len(found), "min": mn, "unreadable_sources": unreadable},
               f"Provide an evidence source stating one of: {', '.join(keys)}." + (f" Unreadable sources were supplied: {', '.join(unreadable)}." if unreadable else ""))
        elif mx is not None and len(found) > mx:
            ob(oid, "role", "INVALID_EVIDENCE", "cardinality_exceeded", {**wit, "found": len(found), "max": mx},
               f"The purpose allows at most {mx} statement(s) of this role; supply exactly the intended one.")
        else:
            ob(oid, "role", "SUPPORTED", "role_satisfied", wit)
            for s, _ in found:
                supplying[s["artifact_id"]] = s
    satisfied = bool(supplying)

    # ---- coherence: do the supplying sources belong together?
    for c in profile.body.get("coherence", []):
        oid = f"coherence:{c['id']}"
        if not satisfied:
            ob(oid, "coherence", "INSUFFICIENT_EVIDENCE", "not_executed_no_role_satisfied", {},
               "Satisfy at least one role first; coherence compares the sources that supplied roles.")
            continue
        vals, missing = [], []
        for s in supplying.values():
            hit = [f for f in s["facts"] if f["key"] == c["fact"]]
            if len(hit) == 1:
                vals.append(_obs(s, hit[0]))
            else:
                missing.append(s["name"])
        distinct = {canonical_bytes(v["value"]) for v in vals}
        if missing:
            ob(oid, "coherence", "INSUFFICIENT_EVIDENCE", "generation_unstated", {"fact": c["fact"], "values": vals, "missing_in": sorted(missing)},
               f"Each source that supplies a role must state '{c['fact']}'; without it the sources cannot be shown to belong together.")
        elif len(distinct) > 1:
            ob(oid, "coherence", "CONTRADICTED", "sources_from_different_generations", {"fact": c["fact"], "values": vals},
               "Supply role evidence that all comes from one generation. Individually valid sources from different generations do not form a valid group.")
        else:
            ob(oid, "coherence", "SUPPORTED", "sources_coherent", {"fact": c["fact"], "values": vals})

    # ---- lifecycle prerequisites and current validity
    lc = profile.body.get("lifecycle")
    if lc:
        ob_l = _lifecycle(profile, lc, usable)
        obligations.extend(ob_l)

    essential = [o for o in obligations]
    verdicts = [o["verdict"] for o in essential]
    if "CONTRADICTED" in verdicts:
        overall = "CONTRADICTED"
    elif "INVALID_EVIDENCE" in verdicts:
        overall = "INVALID_EVIDENCE"
    elif any(v not in ("SUPPORTED",) for v in verdicts) or not verdicts:
        overall = "INSUFFICIENT_EVIDENCE"
    else:
        overall = "SUPPORTED"
    names = {s["artifact_id"]: s["name"] for s in sources}
    out_of_scope = sorted(names[a] for a in names if a not in supplying and not any(
        o["kind"] == "lifecycle" and a in {x.get("artifact_id") for x in o["witness"].get("events", [])} for o in obligations))
    return {"profile": profile.ref(), "bindings": bindings, "overall": overall, "obligations": obligations,
            "scope": {"evaluated_sources": sorted(names[a] for a in supplying), "unreadable_sources": sorted(unreadable),
                      "sources_outside_evaluation_scope": out_of_scope,
                      "statement": "Findings cover only the obligations listed; sources outside scope are not verified by this purpose."},
            "would_resolve": [o["would_resolve"] for o in obligations if o["verdict"] != "SUPPORTED" and o["would_resolve"]]}


def _lifecycle(profile, lc, usable) -> list[dict]:
    base = lc["history"]
    pat = re.compile(rf"^{re.escape(base)}\[(\d+)\]\.(stage|seq|clock)$")
    events, dup = [], []
    for s in usable:
        by_idx: dict[int, dict] = {}
        for f in s["facts"]:
            m = pat.match(f["key"])
            if m:
                by_idx.setdefault(int(m.group(1)), {})[m.group(2)] = f
        for i, parts in sorted(by_idx.items()):
            if {"stage", "seq", "clock"} <= set(parts) and parts["seq"]["value"]["kind"] == "integer" and parts["stage"]["value"]["kind"] == "string" \
                    and parts["clock"]["value"]["kind"] == "string":
                events.append({"artifact_id": s["artifact_id"], "source": s["name"], "index": i, "stage": parts["stage"]["value"]["value"],
                               "seq": int(parts["seq"]["value"]["value"]), "clock": parts["clock"]["value"]["value"],
                               "obs": [_obs(s, parts[k]) for k in ("stage", "seq", "clock")]})
            else:
                dup.append({"source": s["name"], "index": i, "problem": "incomplete or mistyped lifecycle record"})
    out = []

    def ob(oid, verdict, reason, witness, needs=""):
        out.append({"id": oid, "kind": "lifecycle", "verdict": verdict, "reason": reason, "witness": witness, "would_resolve": needs})
    if dup:
        ob("lifecycle:records", "INVALID_EVIDENCE", "malformed_lifecycle_record", {"records": dup, "events": []},
           "Each lifecycle record needs a string stage, an integer seq and a string clock.")
        return out
    seen: dict[tuple, str] = {}
    for e in events:
        k = (e["clock"], e["seq"])
        if k in seen and seen[k] != e["stage"]:
            ob("lifecycle:records", "INVALID_EVIDENCE", "duplicate_lifecycle_entry",
               {"events": [{k2: v for k2, v in x.items() if k2 != "obs"} for x in events if (x["clock"], x["seq"]) == k]},
               "A (clock, seq) pair may record only one stage.")
            return out
        seen[k] = e["stage"]
    have = {e["stage"] for e in events}
    target, requires = lc["target"], lc.get("requires", {})
    closure, stack = set(), [target]
    while stack:
        n = stack.pop()
        for m in requires.get(n, []):
            if m not in closure:
                closure.add(m)
                stack.append(m)
    missing = sorted(s for s in closure | {target} if s not in have)
    flat = [{k: v for k, v in e.items() if k != "obs"} for e in events]
    if missing:
        ob("lifecycle:prerequisites", "INSUFFICIENT_EVIDENCE", "prerequisite_missing", {"missing": missing, "events": flat},
           f"Provide lifecycle records for: {', '.join(missing)}. A boolean field claiming a stage is an assertion, not a record.")
        return out
    ob("lifecycle:prerequisites", "SUPPORTED", "prerequisites_recorded", {"stages": sorted(closure | {target}), "events": flat})
    inval = set(lc.get("invalidating", []))
    last_target = [e for e in events if e["stage"] == target]
    invs = [e for e in events if e["stage"] in inval]
    if not invs:
        ob("lifecycle:current_validity", "SUPPORTED", "no_invalidation_recorded", {"events": flat})
        return out
    amb = [(t, i) for t in last_target for i in invs if t["clock"] != i["clock"]]
    later = [(t, i) for t in last_target for i in invs if t["clock"] == i["clock"] and i["seq"] > t["seq"]]
    earlier_only = all(i["clock"] == t["clock"] and i["seq"] < t["seq"] for t in last_target for i in invs)
    if later and not amb:
        ob("lifecycle:current_validity", "CONTRADICTED", "invalidated_after_success", {"events": flat},
           "Record a new verified success after the invalidation, on the same clock.")
    elif amb or not earlier_only:
        ob("lifecycle:current_validity", "INSUFFICIENT_EVIDENCE", "ordering_ambiguous", {"events": flat, "competing": [[t["stage"], i["stage"]] for t, i in (amb or later)]},
           "Different clocks cannot be ordered without a verified mapping; supply ordering evidence. Both records are retained.")
    else:
        ob("lifecycle:current_validity", "SUPPORTED", "success_after_invalidation", {"events": flat})
    return out

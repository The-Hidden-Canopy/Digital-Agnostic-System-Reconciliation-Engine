"""Structural validation of Bundle v1 with stable, sorted diagnostics (chapter 01)."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import PurePosixPath

from .canonical import (CanonicalError, canonical_bytes, parse_digest, parse_record_id,
                        record_id, strict_loads)
from .model import (SCHEMA_VERSION, Activity, Artifact, Assumption, Bundle, Claim, ProfileRef)


@dataclass(frozen=True, order=True)
class Diagnostic:
    location: str
    code: str
    message: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"location": self.location, "code": self.code, "message": self.message}


@dataclass(frozen=True)
class Limits:
    max_manifest_bytes: int = 4_000_000
    max_depth: int = 32
    max_array: int = 20_000
    max_string: int = 200_000
    max_artifacts: int = 5_000


TOP = ("schema_version", "bundle_id", "artifacts", "claims", "activities",
       "assumptions", "profile_refs")


def safe_relative_path(path: str) -> bool:
    if not isinstance(path, str) or not path or "\x00" in path or "\\" in path:
        return False
    if path.startswith("/") or (len(path) > 1 and path[1] == ":"):
        return False
    parts = PurePosixPath(path).parts
    return bool(parts) and "/".join(parts) == path and all(p not in ("", ".", "..") for p in parts)


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def parse_bundle(data: bytes, limits: Limits = Limits()) -> tuple[Bundle | None, list[Diagnostic]]:
    """Return a fully validated bundle or diagnostics - never a partial bundle."""
    diags: list[Diagnostic] = []
    try:
        raw = strict_loads(data, max_bytes=limits.max_manifest_bytes, max_depth=limits.max_depth)
    except CanonicalError as exc:
        return None, [Diagnostic("", exc.code, str(exc)[:200])]
    if not isinstance(raw, dict):
        return None, [Diagnostic("", "not_object")]
    for key in TOP:
        if key not in raw:
            diags.append(Diagnostic(f"/{key}", "missing_field"))
    for key in raw:
        if key not in TOP:
            diags.append(Diagnostic(f"/{key}", "unknown_field"))
    if diags:
        return None, sorted(diags)
    if raw["schema_version"] != SCHEMA_VERSION:
        return None, [Diagnostic("/schema_version", "unsupported_version")]
    for key in TOP[2:]:
        if not isinstance(raw[key], list):
            diags.append(Diagnostic(f"/{key}", "not_array"))
        elif len(raw[key]) > limits.max_array or (key == "artifacts" and len(raw[key]) > limits.max_artifacts):
            diags.append(Diagnostic(f"/{key}", "array_limit"))
    if diags:
        return None, sorted(diags)

    def exact(rec, loc, fields):
        if not isinstance(rec, dict):
            diags.append(Diagnostic(loc, "not_object"))
            return False
        ok = True
        for f in fields:
            if f not in rec:
                diags.append(Diagnostic(f"{loc}/{f}", "missing_field"))
                ok = False
        for f in rec:
            if f not in fields:
                diags.append(Diagnostic(f"{loc}/{f}", "unknown_field"))
                ok = False
        return ok

    def strs(rec, loc, field):
        v = rec.get(field)
        if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
            diags.append(Diagnostic(f"{loc}/{field}", "not_string_array"))
            return None
        return tuple(v)

    ids: dict[str, str] = {}

    def check_id(kind, rec, loc, body_fields):
        rid = rec.get("id")
        if not parse_record_id(kind, rid):
            diags.append(Diagnostic(f"{loc}/id", "malformed_id"))
            return
        if rid in ids:
            diags.append(Diagnostic(f"{loc}/id", "duplicate_id", rid))
        ids[rid] = kind
        body = {k: rec[k] for k in body_fields}
        try:
            if record_id(kind, body) != rid:
                diags.append(Diagnostic(f"{loc}/id", "id_mismatch"))
        except CanonicalError:
            diags.append(Diagnostic(loc, "noncanonical_value"))

    artifacts, claims, activities, assumptions, profiles = [], [], [], [], []
    af = ("id", "digest", "media_type", "size_bytes", "relative_path")
    for i, rec in enumerate(raw["artifacts"]):
        loc = f"/artifacts/{i}"
        if not exact(rec, loc, af):
            continue
        if not parse_digest(rec["digest"]):
            diags.append(Diagnostic(f"{loc}/digest", "malformed_digest"))
        if not isinstance(rec["media_type"], str) or not rec["media_type"]:
            diags.append(Diagnostic(f"{loc}/media_type", "bad_type"))
        if not _is_int(rec["size_bytes"]) or rec["size_bytes"] < 0:
            diags.append(Diagnostic(f"{loc}/size_bytes", "bad_integer"))
        if not safe_relative_path(rec["relative_path"]):
            diags.append(Diagnostic(f"{loc}/relative_path", "unsafe_path"))
        check_id("artifact", rec, loc, af[1:])
        if not any(d.location.startswith(loc) for d in diags):
            artifacts.append(Artifact(rec["id"], rec["digest"], rec["media_type"],
                                      rec["size_bytes"], rec["relative_path"]))
    cf = ("id", "predicate", "subject", "arguments", "evidence_refs", "assumption_refs")
    for i, rec in enumerate(raw["claims"]):
        loc = f"/claims/{i}"
        if not exact(rec, loc, cf):
            continue
        for f in ("predicate", "subject"):
            if not isinstance(rec[f], str) or not rec[f]:
                diags.append(Diagnostic(f"{loc}/{f}", "bad_string"))
        ev, asm = strs(rec, loc, "evidence_refs"), strs(rec, loc, "assumption_refs")
        check_id("claim", rec, loc, cf[1:])
        if ev is not None and asm is not None and not any(d.location.startswith(loc) for d in diags):
            claims.append(Claim(rec["id"], rec["predicate"], rec["subject"],
                                canonical_bytes(rec["arguments"]).decode(), ev, asm))
    acf = ("id", "operation", "inputs", "outputs", "depends_on", "generation", "logical_clock")
    for i, rec in enumerate(raw["activities"]):
        loc = f"/activities/{i}"
        if not exact(rec, loc, acf):
            continue
        ins, outs, deps = (strs(rec, loc, f) for f in ("inputs", "outputs", "depends_on"))
        clock = rec["logical_clock"]
        if clock is not None:
            if not (isinstance(clock, dict) and set(clock) == {"clock_id", "tick"}
                    and isinstance(clock["clock_id"], str) and _is_int(clock["tick"]) and clock["tick"] >= 0):
                diags.append(Diagnostic(f"{loc}/logical_clock", "bad_clock"))
        check_id("activity", rec, loc, acf[1:])
        if None not in (ins, outs, deps) and not any(d.location.startswith(loc) for d in diags):
            activities.append(Activity(rec["id"], rec["operation"], ins, outs, deps,
                                       rec["generation"],
                                       None if clock is None else canonical_bytes(clock).decode()))
    asf = ("id", "statement", "scope")
    for i, rec in enumerate(raw["assumptions"]):
        loc = f"/assumptions/{i}"
        if not exact(rec, loc, asf):
            continue
        check_id("assumption", rec, loc, asf[1:])
        if not any(d.location.startswith(loc) for d in diags):
            assumptions.append(Assumption(rec["id"], rec["statement"], rec["scope"]))
    for i, rec in enumerate(raw["profile_refs"]):
        loc = f"/profile_refs/{i}"
        if exact(rec, loc, ("profile_id", "profile_version", "digest")):
            if not parse_digest(rec["digest"]):
                diags.append(Diagnostic(f"{loc}/digest", "malformed_digest"))
            else:
                profiles.append(ProfileRef(rec["profile_id"], rec["profile_version"], rec["digest"]))

    # reference resolution
    art_ids = {a.id for a in artifacts}
    act_ids = {a.id for a in activities}
    asm_ids = {a.id for a in assumptions}
    paths: set[str] = set()
    for i, a in enumerate(artifacts):
        if a.relative_path in paths:
            diags.append(Diagnostic(f"/artifacts/{i}/relative_path", "duplicate_path"))
        paths.add(a.relative_path)
    for i, c in enumerate(claims):
        for r in c.evidence_refs:
            if r not in art_ids and r not in act_ids:
                diags.append(Diagnostic(f"/claims/{i}/evidence_refs", "dangling_reference", r))
        for r in c.assumption_refs:
            if r not in asm_ids:
                diags.append(Diagnostic(f"/claims/{i}/assumption_refs", "dangling_reference", r))
    for i, a in enumerate(activities):
        for r in a.inputs + a.outputs:
            if r not in art_ids:
                diags.append(Diagnostic(f"/activities/{i}/inputs", "dangling_reference", r))
        for r in a.depends_on:
            if r not in act_ids:
                diags.append(Diagnostic(f"/activities/{i}/depends_on", "dangling_reference", r))
    if diags:
        return None, sorted(set(diags))
    bundle = Bundle(SCHEMA_VERSION, raw["bundle_id"], tuple(artifacts), tuple(claims),
                    tuple(activities), tuple(assumptions), tuple(profiles))
    if not parse_record_id("bundle", raw["bundle_id"]):
        return None, [Diagnostic("/bundle_id", "malformed_id")]
    if bundle.compute_id() != raw["bundle_id"]:
        return None, [Diagnostic("/bundle_id", "id_mismatch")]
    return bundle, []

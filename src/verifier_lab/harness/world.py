"""Synthetic scenarios and the kernels that judge them (the real one and deliberately broken controls)."""
from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from typing import Protocol

from ..canonical import canonical_bytes, sha256_digest
from ..engine import CheckPlan, check_claims
from ..ingestion.common import Limits
from ..ingestion.extract import build_record, extract_local
from ..model import Activity, Artifact, Bundle, Claim
from ..rules import (EXTRACT_OP, EXTRACTION_MEDIA, PROVENANCE_MEDIA, CheckContext, builtin_registry)

L = Limits()
FACT_PREDICATES = ("fact.equals", "fact.numeric_bound", "fact.present")


@dataclass(frozen=True)
class Doc:
    name: str
    fmt: str                 # "json" | "markdown"
    data: bytes

    def to_dict(self) -> dict:
        return {"name": self.name, "fmt": self.fmt, "text": self.data.decode("utf-8", errors="backslashreplace"),
                "sha256": sha256_digest(self.data)}


@dataclass(frozen=True)
class ClaimSpec:
    key: str                  # stable local name, e.g. "c_limit"
    predicate: str
    subject: str
    arguments: dict

    def touch_keys(self) -> tuple[str, ...]:
        if self.predicate == "interval.relation":
            a = self.arguments
            return tuple(f"{n}.{x}" for n in (a["left"], a["right"]) for x in ("start", "end"))
        return (self.subject,)

    def to_dict(self) -> dict:
        return {"key": self.key, "predicate": self.predicate, "subject": self.subject, "arguments": self.arguments}


@dataclass(frozen=True)
class Scenario:
    id: str
    origin: Doc
    evidence: tuple[Doc, ...]
    claims: tuple[ClaimSpec, ...]

    def to_dict(self) -> dict:
        return {"id": self.id, "origin": self.origin.to_dict(), "evidence": [d.to_dict() for d in self.evidence],
                "claims": [c.to_dict() for c in self.claims]}

    def digest(self) -> str:
        return sha256_digest(canonical_bytes(self.to_dict()))

    def claim(self, key: str) -> ClaimSpec:
        return next(c for c in self.claims if c.key == key)


Outcome = tuple[str, str]        # (verdict, reason_code)


class Kernel(Protocol):
    name: str

    def verify(self, scenario: Scenario) -> dict[str, Outcome]: ...


class RealKernel:
    """The production rules and engine, assembled in memory from the scenario (no workspace, no UI)."""
    name = "real"

    def verify(self, scenario: Scenario) -> dict[str, Outcome]:
        blobs: dict[str, bytes] = {}
        arts: dict[str, Artifact] = {}
        acts: list[Activity] = []

        def add(data: bytes, media: str) -> Artifact:
            d = sha256_digest(data)
            a = Artifact.make(d, media, len(data), "artifacts/" + d[7:])
            blobs[d], arts[a.id] = data, a
            return a

        def ingest(doc: Doc) -> Artifact:
            wire = extract_local(doc.fmt, doc.data, None, L)
            rec, rend = build_record(sha256_digest(doc.data), doc.data, wire, L, {})
            src = add(doc.data, "application/" + doc.fmt)
            if rec["rendition_digest"] != sha256_digest(doc.data):
                add(rend, "text/plain")
            out = add(canonical_bytes(rec), EXTRACTION_MEDIA)
            acts.append(Activity.make(EXTRACT_OP, [src.id], [out.id], [], rec["manifest"]["id"]))
            return src

        origin = ingest(scenario.origin)
        ev_arts = [ingest(d) for d in scenario.evidence]
        prov = add(canonical_bytes({"origin_source_artifact_id": origin.id}), PROVENANCE_MEDIA)
        claims, names = [], {}
        for spec in scenario.claims:
            c = Claim.make(spec.predicate, spec.subject, spec.arguments, [prov.id] + [a.id for a in ev_arts], [])
            claims.append(c)
            names[c.id] = spec.key
        bundle = Bundle.build(arts.values(), claims, acts, [], [])
        reg = builtin_registry()
        run = check_claims(CheckContext(bundle, lambda a: blobs[a.digest]), bundle.claims,
                           CheckPlan.from_registry(reg), reg)
        if run.errors:
            raise RuntimeError(f"execution errors: {[e.code for _, e in run.errors]}")
        return {names[f.claim_id]: (f.verdict.value, f.reason_code) for f in run.findings}


# ----------------------------------------------------------------- deliberately defective kernels (negative controls)
class AlwaysSupported:
    name = "always_supported"

    def verify(self, scenario):
        return {c.key: ("SUPPORTED", "value_equal") for c in scenario.claims}


class AlwaysContradicted:
    name = "always_contradicted"

    def verify(self, scenario):
        return {c.key: ("CONTRADICTED", "value_mismatch") for c in scenario.claims}


class IgnoresConflictsAndDuplicates:
    """Subtle: the real kernel for ordinary cases, but 'last key wins' and 'first source wins' on conflicts."""
    name = "last_key_wins"

    def verify(self, scenario):
        def last_wins(doc: Doc) -> Doc:
            if doc.fmt != "json":
                return doc
            try:
                return Doc(doc.name, doc.fmt, json.dumps(json.loads(doc.data), indent=1).encode())   # silently dedups
            except ValueError:
                return doc
        s = Scenario(scenario.id, last_wins(scenario.origin), tuple(last_wins(d) for d in scenario.evidence[:1]), scenario.claims)
        return RealKernel().verify(s)


class NoSelfReferenceCheck:
    """Subtle: lets an assertion count as its own evidence (a classic circular-support bug)."""
    name = "self_reference_allowed"

    def verify(self, scenario):
        if len(scenario.evidence) == 1 and scenario.evidence[0].data == scenario.origin.data:
            return {c.key: ("SUPPORTED", "value_equal") for c in scenario.claims}
        return RealKernel().verify(scenario)


KERNELS = {k.name: k for k in (RealKernel(), AlwaysSupported(), AlwaysContradicted(), IgnoresConflictsAndDuplicates(), NoSelfReferenceCheck())}


# ------------------------------------------------------------------------------------ base scenarios
def _j(obj) -> bytes:
    return json.dumps(obj, indent=1).encode()


def base_scenarios() -> list[Scenario]:
    """Hand-authored bases with manually specified expected verdicts (every claim is SUPPORTED here)."""
    origin = Doc("origin.md", "markdown", b"---\nnote: neutral origin\n---\n")
    limits = Scenario("S-limits", origin, (Doc("cfg.json", "json", _j({"limits": {"max": 200, "retries": 3}, "name": "svc"})),), (
        ClaimSpec("c_eq", "fact.equals", "limits.max", {"expected": {"kind": "integer", "value": "200"}}),
        ClaimSpec("c_bound", "fact.numeric_bound", "limits.retries", {"op": "lte", "bound": {"kind": "integer", "value": "5"}}),
        ClaimSpec("c_present", "fact.present", "name", {}),
        ClaimSpec("c_name", "fact.equals", "name", {"expected": {"kind": "string", "value": "svc"}})))
    sched = Scenario("S-schedule", origin, (Doc("log.json", "json", _j({"a": {"start": 0, "end": 5}, "b": {"start": 5, "end": 9}})),), (
        ClaimSpec("c_prec", "interval.relation", "a-before-b", {"left": "a", "right": "b", "relation": "precedes", "coord": "s"}),
        ClaimSpec("c_noov", "interval.relation", "a-b-disjoint", {"left": "a", "right": "b", "relation": "no_overlap", "coord": "s"})))
    return [limits, sched]


BASE_EXPECTED = "SUPPORTED"

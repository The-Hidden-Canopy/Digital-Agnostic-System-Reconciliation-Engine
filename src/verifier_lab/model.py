"""Immutable core records (chapter 01). Bundle v1 carries no application-layer fields."""
from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .canonical import JsonValue, canonical_bytes, record_id

SCHEMA_VERSION = "1"


class Verdict(StrEnum):
    SUPPORTED = "SUPPORTED"
    CONTRADICTED = "CONTRADICTED"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    INVALID_EVIDENCE = "INVALID_EVIDENCE"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    HUMAN_REVIEW_REQUIRED = "HUMAN_REVIEW_REQUIRED"


def _freeze(value: JsonValue) -> str:
    return canonical_bytes(value).decode("utf-8")


@dataclass(frozen=True)
class Artifact:
    id: str
    digest: str
    media_type: str
    size_bytes: int
    relative_path: str

    def body(self) -> dict[str, JsonValue]:
        return {"digest": self.digest, "media_type": self.media_type,
                "size_bytes": self.size_bytes, "relative_path": self.relative_path}

    @staticmethod
    def make(digest: str, media_type: str, size_bytes: int, relative_path: str) -> "Artifact":
        body = {"digest": digest, "media_type": media_type,
                "size_bytes": size_bytes, "relative_path": relative_path}
        return Artifact(record_id("artifact", body), digest, media_type, size_bytes, relative_path)

    def to_dict(self) -> dict[str, JsonValue]:
        return {"id": self.id, **self.body()}


@dataclass(frozen=True)
class Claim:
    id: str
    predicate: str
    subject: str
    arguments_json: str
    evidence_refs: tuple[str, ...]
    assumption_refs: tuple[str, ...]

    @property
    def arguments(self) -> JsonValue:
        return json.loads(self.arguments_json)

    @staticmethod
    def make(predicate: str, subject: str, arguments: JsonValue,
             evidence_refs, assumption_refs) -> "Claim":
        ev = tuple(sorted(set(evidence_refs)))
        asm = tuple(sorted(set(assumption_refs)))
        body = {"predicate": predicate, "subject": subject, "arguments": arguments,
                "evidence_refs": list(ev), "assumption_refs": list(asm)}
        return Claim(record_id("claim", body), predicate, subject, _freeze(arguments), ev, asm)

    def to_dict(self) -> dict[str, JsonValue]:
        return {"id": self.id, "predicate": self.predicate, "subject": self.subject,
                "arguments": self.arguments, "evidence_refs": list(self.evidence_refs),
                "assumption_refs": list(self.assumption_refs)}


@dataclass(frozen=True)
class Activity:
    id: str
    operation: str
    inputs: tuple[str, ...]
    outputs: tuple[str, ...]
    depends_on: tuple[str, ...]
    generation: str
    logical_clock: str | None  # canonical JSON of {"clock_id","tick"} or None

    @staticmethod
    def make(operation, inputs, outputs, depends_on, generation, logical_clock=None) -> "Activity":
        ins, outs, deps = (tuple(sorted(set(x))) for x in (inputs, outputs, depends_on))
        body = {"operation": operation, "inputs": list(ins), "outputs": list(outs),
                "depends_on": list(deps), "generation": generation, "logical_clock": logical_clock}
        return Activity(record_id("activity", body), operation, ins, outs, deps, generation,
                        None if logical_clock is None else _freeze(logical_clock))

    def to_dict(self) -> dict[str, JsonValue]:
        return {"id": self.id, "operation": self.operation, "inputs": list(self.inputs),
                "outputs": list(self.outputs), "depends_on": list(self.depends_on),
                "generation": self.generation,
                "logical_clock": None if self.logical_clock is None else json.loads(self.logical_clock)}


@dataclass(frozen=True)
class Assumption:
    id: str
    statement: str
    scope: str

    @staticmethod
    def make(statement: str, scope: str) -> "Assumption":
        return Assumption(record_id("assumption", {"statement": statement, "scope": scope}),
                          statement, scope)

    def to_dict(self) -> dict[str, JsonValue]:
        return {"id": self.id, "statement": self.statement, "scope": self.scope}


@dataclass(frozen=True)
class ProfileRef:
    profile_id: str
    profile_version: str
    digest: str

    def to_dict(self) -> dict[str, JsonValue]:
        return {"profile_id": self.profile_id, "profile_version": self.profile_version,
                "digest": self.digest}


@dataclass(frozen=True)
class Bundle:
    schema_version: str
    bundle_id: str
    artifacts: tuple[Artifact, ...]
    claims: tuple[Claim, ...]
    activities: tuple[Activity, ...]
    assumptions: tuple[Assumption, ...]
    profile_refs: tuple[ProfileRef, ...]

    @staticmethod
    def build(artifacts, claims, activities, assumptions, profile_refs) -> "Bundle":
        a = tuple(sorted(artifacts, key=lambda x: x.id))
        c = tuple(sorted(claims, key=lambda x: x.id))
        act = tuple(sorted(activities, key=lambda x: x.id))
        asm = tuple(sorted(assumptions, key=lambda x: x.id))
        pr = tuple(sorted(profile_refs, key=lambda x: (x.profile_id, x.profile_version)))
        bundle = Bundle(SCHEMA_VERSION, "", a, c, act, asm, pr)
        return Bundle(SCHEMA_VERSION, bundle.compute_id(), a, c, act, asm, pr)

    def _body(self) -> dict[str, JsonValue]:
        return {"schema_version": self.schema_version,
                "artifacts": [x.to_dict() for x in self.artifacts],
                "claims": [x.to_dict() for x in self.claims],
                "activities": [x.to_dict() for x in self.activities],
                "assumptions": [x.to_dict() for x in self.assumptions],
                "profile_refs": [x.to_dict() for x in self.profile_refs]}

    def compute_id(self) -> str:
        return record_id("bundle", self._body())

    def to_dict(self) -> dict[str, JsonValue]:
        return {**self._body(), "bundle_id": self.bundle_id}

    def artifact(self, artifact_id: str) -> Artifact | None:
        return next((a for a in self.artifacts if a.id == artifact_id), None)


@dataclass(frozen=True)
class Finding:
    id: str
    claim_id: str
    verdict: Verdict
    rule_id: str
    rule_version: str
    evidence_refs: tuple[str, ...]
    assumption_refs: tuple[str, ...]
    reason_code: str
    witness_json: str | None

    @property
    def witness(self) -> JsonValue:
        return None if self.witness_json is None else json.loads(self.witness_json)

    @staticmethod
    def make(claim_id, verdict, rule_id, rule_version, evidence_refs, assumption_refs,
             reason_code, witness=None) -> "Finding":
        ev = tuple(sorted(set(evidence_refs)))
        asm = tuple(sorted(set(assumption_refs)))
        body = {"claim_id": claim_id, "verdict": str(Verdict(verdict)), "rule_id": rule_id,
                "rule_version": rule_version, "evidence_refs": list(ev),
                "assumption_refs": list(asm), "reason_code": reason_code, "witness": witness}
        return Finding(record_id("finding", body), claim_id, Verdict(verdict), rule_id,
                       rule_version, ev, asm, reason_code,
                       None if witness is None else _freeze(witness))

    def to_dict(self) -> dict[str, JsonValue]:
        return {"id": self.id, "claim_id": self.claim_id, "verdict": str(self.verdict),
                "rule_id": self.rule_id, "rule_version": self.rule_version,
                "evidence_refs": list(self.evidence_refs),
                "assumption_refs": list(self.assumption_refs),
                "reason_code": self.reason_code, "witness": self.witness}

    def expected_id(self) -> str:
        body = self.to_dict()
        del body["id"]
        return record_id("finding", body)


@dataclass(frozen=True)
class ExecutionError:
    """Operational failure; never a verdict (chapter 00)."""
    code: str
    operation: str
    message: str
    retryable: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "operation": self.operation,
                "message": self.message[:500], "retryable": self.retryable}

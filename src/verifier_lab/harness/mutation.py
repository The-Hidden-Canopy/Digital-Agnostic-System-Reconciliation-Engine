"""Mutation operators (chapter 09). Semantic operators rebuild every dependent value so they exercise the *rule*,
not merely an integrity check; byte-level operators deliberately damage serialization and test the ingestion boundary.

Expected outcomes are committed data in each operator, never derived from what a kernel returned.
"""
from __future__ import annotations

import json
import random
from dataclasses import dataclass

from ..canonical import sha256_digest
from .world import ClaimSpec, Doc, Scenario

GENERATOR_VERSION = "mut-1"
INT_OP, STR_OP = "int", "str"


@dataclass(frozen=True)
class MutationSpec:
    mutation_id: str
    target_ref: str
    operator: str
    parameters: dict
    expected_relation: str

    def to_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass(frozen=True)
class MutationRecord:
    base_digest: str
    result_digest: str
    generator_version: str
    seed: str
    applied: MutationSpec

    def to_dict(self) -> dict:
        return {"base_digest": self.base_digest, "result_digest": self.result_digest,
                "generator_version": self.generator_version, "seed": self.seed, "applied": self.applied.to_dict()}


@dataclass(frozen=True)
class Mutated:
    scenario: Scenario
    touched: frozenset[str]            # fact keys whose evidence was changed
    record: MutationRecord


def _path(key: str) -> list[str]:
    return key.split(".")


def _get(obj, path):
    for p in path:
        if not isinstance(obj, dict) or p not in obj:
            return None, False
        obj = obj[p]
    return obj, True


def _json_docs(s: Scenario):
    for i, d in enumerate(s.evidence):
        if d.fmt == "json":
            try:
                yield i, d, json.loads(d.data)
            except ValueError:
                continue


def _find(s: Scenario, key: str):
    """First evidence doc holding `key`."""
    for i, d, obj in _json_docs(s):
        v, ok = _get(obj, _path(key))
        if ok:
            return i, d, obj, v
    return None


def _replace(s: Scenario, i: int, doc: Doc) -> Scenario:
    ev = list(s.evidence)
    ev[i] = doc
    return Scenario(s.id, s.origin, tuple(ev), s.claims)


def _dump(obj) -> bytes:
    return json.dumps(obj, indent=1).encode()


def _set(obj, path, value):
    for p in path[:-1]:
        obj = obj[p]
    obj[path[-1]] = value


def _del(obj, path):
    for p in path[:-1]:
        obj = obj[p]
    del obj[path[-1]]


def _bound_keys(c: ClaimSpec) -> list[str]:
    return list(c.touch_keys())


class Operator:
    id: str = ""
    layer: str = "semantic"
    soundness: str = ""
    affects_all: bool = False

    def applies(self, s: Scenario, c: ClaimSpec) -> bool:
        raise NotImplementedError

    def apply(self, s: Scenario, c: ClaimSpec, rng: random.Random) -> tuple[Scenario, set[str], dict]:
        raise NotImplementedError

    def expected(self, c: ClaimSpec) -> tuple[str, str]:
        raise NotImplementedError


class RemoveFact(Operator):
    id = "remove_required_evidence"
    soundness = "Removing the only statement of a required fact cannot leave the claim supported; absence is not a negative."

    def applies(self, s, c):
        return all(_find(s, k) for k in _bound_keys(c)) and len(s.evidence) == 1

    def apply(self, s, c, rng):
        key = _bound_keys(c)[-1] if c.predicate == "interval.relation" else c.subject
        i, d, obj, _ = _find(s, key)
        _del(obj, _path(key))
        return _replace(s, i, Doc(d.name, d.fmt, _dump(obj))), {key}, {"removed": key}

    def expected(self, c):
        return ("INSUFFICIENT_EVIDENCE", "bound_missing" if c.predicate == "interval.relation" else "fact_not_found")


class ChangeValue(Operator):
    id = "change_value"
    soundness = "Changing the evidenced value so it disagrees with the claim must yield a witnessed contradiction."

    def applies(self, s, c):
        if c.predicate == "fact.present" or not all(_find(s, k) for k in _bound_keys(c)):
            return False
        hit = _find(s, c.subject if c.predicate != "interval.relation" else c.touch_keys()[0])
        return hit is not None and len(s.evidence) == 1

    def apply(self, s, c, rng):
        n = rng.randint(1, 3)
        if c.predicate == "interval.relation":
            a = c.arguments
            key = f"{a['left']}.end"
            i, d, obj, _ = _find(s, key)
            b_start, _ok = _get(obj, [a["right"], "start"])
            _set(obj, _path(key), b_start + n)            # left now ends after right has started
        else:
            key = c.subject
            i, d, obj, v = _find(s, key)
            if c.predicate == "fact.numeric_bound":
                v = int(c.arguments["bound"]["value"]) + n          # exceed the bound
            elif isinstance(v, str):
                v = v + "x" * n
            else:
                v = v + n
            _set(obj, _path(key), v)
        return _replace(s, i, Doc(d.name, d.fmt, _dump(obj))), {key}, {"delta": n}

    def expected(self, c):
        return ("CONTRADICTED", "bound_violated" if c.predicate == "fact.numeric_bound"
                else "relation_violated" if c.predicate == "interval.relation" else "value_mismatch")


class ConflictingSource(Operator):
    id = "conflicting_second_source"
    soundness = "Two valid sources disagreeing is a conflict to surface, not a contradiction to pick a winner for."

    def applies(self, s, c):
        return c.predicate in ("fact.equals", "fact.numeric_bound") and _find(s, c.subject) is not None and len(s.evidence) == 1

    def apply(self, s, c, rng):
        i, d, obj, v = _find(s, c.subject)
        other = json.loads(d.data)
        if c.predicate == "fact.numeric_bound":
            new = int(c.arguments["bound"]["value"]) + 1 + rng.randint(0, 2)   # violates the bound
        elif isinstance(v, str):
            new = v + "-other"
        else:
            new = v + 1 + rng.randint(0, 2)
        _set(other, _path(c.subject), new)
        doc = Doc("second-" + d.name, d.fmt, _dump(other))
        return Scenario(s.id, s.origin, s.evidence + (doc,), s.claims), {c.subject}, {"other_value": new}

    def expected(self, c):
        return ("HUMAN_REVIEW_REQUIRED", "evidence_conflict")


class DuplicateKey(Operator):
    id = "duplicate_key_in_evidence"
    soundness = "A repeated key is ambiguous evidence; 'last one wins' must never decide it silently."

    def applies(self, s, c):
        key = c.touch_keys()[-1] if c.predicate == "interval.relation" else c.subject
        hit = _find(s, key)
        return hit is not None and len(s.evidence) == 1 and hit[1].data.count(b'"' + _path(key)[-1].encode() + b'":') == 1

    def apply(self, s, c, rng):
        key = c.touch_keys()[-1] if c.predicate == "interval.relation" else c.subject
        i, d, _, _ = _find(s, key)
        leaf = _path(key)[-1].encode()
        lines = d.data.split(b"\n")
        j = next(n for n, ln in enumerate(lines) if ln.lstrip().startswith(b'"' + leaf + b'":'))
        dup = lines[j].rstrip(b",") + b","
        lines.insert(j, dup)
        return _replace(s, i, Doc(d.name, d.fmt, b"\n".join(lines))), {key}, {"duplicated": key}

    def expected(self, c):
        return ("HUMAN_REVIEW_REQUIRED", "evidence_key_ambiguous")


class KindChange(Operator):
    id = "change_value_kind"
    soundness = "A number presented as text is a different kind; it cannot silently count as equal or as a violation."

    def applies(self, s, c):
        if c.predicate == "fact.present":
            return False
        key = c.touch_keys()[1] if c.predicate == "interval.relation" else c.subject
        return _find(s, key) is not None and len(s.evidence) == 1

    def apply(self, s, c, rng):
        key = c.touch_keys()[1] if c.predicate == "interval.relation" else c.subject
        i, d, obj, v = _find(s, key)
        _set(obj, _path(key), str(v) if isinstance(v, int) else 7)
        return _replace(s, i, Doc(d.name, d.fmt, _dump(obj))), {key}, {"to": "string" if isinstance(v, int) else "integer"}

    def expected(self, c):
        return ("HUMAN_REVIEW_REQUIRED", "kind_mismatch")


class SelfReference(Operator):
    id = "assertion_as_its_own_evidence"
    soundness = "A claim's own source can never be independent evidence for it."
    affects_all = True

    def applies(self, s, c):
        return len(s.evidence) >= 1 and s.evidence[0].fmt == "json"

    def apply(self, s, c, rng):
        d = s.evidence[0]
        keys = {k for cc in s.claims for k in cc.touch_keys()}
        return Scenario(s.id, d, (d,), s.claims), keys, {}

    def expected(self, c):
        return ("INSUFFICIENT_EVIDENCE", "no_independent_evidence")


class TruncateBytes(Operator):
    id = "truncate_evidence_bytes"
    layer = "byte"
    soundness = "Truncated structured evidence is unreadable; it must be INVALID_EVIDENCE, not 'not found'."
    affects_all = True

    def applies(self, s, c):
        return len(s.evidence) == 1 and s.evidence[0].fmt == "json"

    def apply(self, s, c, rng):
        d = s.evidence[0]
        keys = {k for cc in s.claims for k in cc.touch_keys()}
        return _replace(s, 0, Doc(d.name, d.fmt, d.data[: max(1, len(d.data) // 2)])), keys, {}

    def expected(self, c):
        return ("INVALID_EVIDENCE", "evidence_unreadable")


class InvalidUtf8(TruncateBytes):
    id = "invalid_utf8_evidence"
    soundness = "Bytes that are not UTF-8 are corrupt evidence."

    def apply(self, s, c, rng):
        d = s.evidence[0]
        keys = {k for cc in s.claims for k in cc.touch_keys()}
        return _replace(s, 0, Doc(d.name, d.fmt, d.data + b"\xff")), keys, {}


class WidenUncertainty(Operator):
    id = "widen_time_uncertainty"
    soundness = "Widening a bound until it straddles the other activity cannot leave a definite temporal conclusion."

    def applies(self, s, c):
        return c.predicate == "interval.relation" and _find(s, f"{c.arguments['left']}.end") is not None and len(s.evidence) == 1

    def apply(self, s, c, rng):
        a = c.arguments
        key = f"{a['left']}.end"
        i, d, obj, v = _find(s, key)
        b_start, _ = _get(obj, [a["right"], "start"])
        _del(obj, _path(key))
        obj[a["left"]]["end_min"] = b_start - 2 - rng.randint(0, 1)
        obj[a["left"]]["end_max"] = b_start + 2 + rng.randint(0, 1)
        return _replace(s, i, Doc(d.name, d.fmt, _dump(obj))), {key}, {}

    def expected(self, c):
        return ("INSUFFICIENT_EVIDENCE", "relation_uncertain")


class ReverseInterval(Operator):
    id = "move_activity_after"
    soundness = "Swapping the two activities reverses a strict 'precedes' relation."

    def applies(self, s, c):
        return c.predicate == "interval.relation" and c.arguments["relation"] == "precedes" and len(s.evidence) == 1 \
            and _find(s, f"{c.arguments['left']}.start") is not None

    def apply(self, s, c, rng):
        a = c.arguments
        i, d, obj, _ = _find(s, f"{a['left']}.start")
        obj[a["left"]], obj[a["right"]] = obj[a["right"]], obj[a["left"]]
        return _replace(s, i, Doc(d.name, d.fmt, _dump(obj))), set(c.touch_keys()), {}

    def expected(self, c):
        return ("CONTRADICTED", "relation_violated")


OPERATORS: tuple[Operator, ...] = (RemoveFact(), ChangeValue(), ConflictingSource(), DuplicateKey(), KindChange(),
                                   SelfReference(), TruncateBytes(), InvalidUtf8(), WidenUncertainty(), ReverseInterval())
OPERATOR_BY_ID = {o.id: o for o in OPERATORS}


def mutate(s: Scenario, claim_key: str, operator_id: str, seed: str) -> Mutated:
    op = OPERATOR_BY_ID[operator_id]
    c = s.claim(claim_key)
    rng = random.Random(f"{seed}|{s.id}|{claim_key}|{operator_id}")     # documented deterministic generator, not security-grade
    new, touched, params = op.apply(s, c, rng)
    spec = MutationSpec(f"{operator_id}:{s.id}:{claim_key}", claim_key, operator_id, params, "target_matches_expected")
    return Mutated(new, frozenset(touched), MutationRecord(s.digest(), new.digest(), GENERATOR_VERSION, seed, spec))

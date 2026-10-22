"""Built-in pure rules over a validated CheckContext (chapters 02, 04). No I/O, no clocks, no randomness."""
from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable, Protocol

from .canonical import CanonicalError, canonical_bytes, sha256_digest, strict_loads
from .ingestion import values as V
from .model import Artifact, Bundle, Claim, Finding, Verdict

EXTRACTION_MEDIA = "application/vnd.verifier-lab.extraction-record+json"
PROVENANCE_MEDIA = "application/vnd.verifier-lab.claim-provenance+json"
RISKPACK_MEDIA = "application/vnd.verifier-lab.riskpack+json"
CONTEXT_MEDIA = "application/vnd.verifier-lab.context-facts+json"
EXTRACT_OP = "ingestion.extract"
USABLE = ("PARSED", "PARTIALLY_PARSED")
MAX_WITNESS_BYTES = 100_000


class CheckContext:
    """Read-only indexed view of an admitted bundle; artifact bytes come through a verified reader."""

    def __init__(self, bundle: Bundle, reader: Callable[[Artifact], bytes]):
        self.bundle = bundle
        self._reader = reader
        self._cache: dict[str, object] = {}

    def artifact_bytes(self, artifact_id: str) -> bytes:
        art = self.bundle.artifact(artifact_id)
        if art is None:
            raise KeyError(artifact_id)
        return self._reader(art)

    def _json(self, artifact_id: str):
        if artifact_id not in self._cache:
            self._cache[artifact_id] = strict_loads(self.artifact_bytes(artifact_id), max_bytes=64_000_000, max_depth=32)
        return self._cache[artifact_id]

    def extraction_for(self, source_artifact_id: str):
        """Return (record, rendition_artifact_id) for an evidence source, or (None, None)."""
        for act in self.bundle.activities:
            if act.operation == EXTRACT_OP and source_artifact_id in act.inputs:
                for out in act.outputs:
                    art = self.bundle.artifact(out)
                    if art and art.media_type == EXTRACTION_MEDIA:
                        rec = self._json(out)
                        rend = next((o for o in act.outputs + act.inputs
                                     if self.bundle.artifact(o).digest == rec["rendition_digest"]), None)
                        return rec, rend
        return None, None

    def provenance(self, claim: Claim):
        for ref in claim.evidence_refs:
            art = self.bundle.artifact(ref)
            if art and art.media_type == PROVENANCE_MEDIA:
                return self._json(ref)
        return None


class Rule(Protocol):
    rule_id: str
    rule_version: str
    predicate: str

    def evaluate(self, context: CheckContext, claim: Claim) -> Finding: ...


def _valid_typed(v) -> bool:
    return (isinstance(v, dict) and set(v) == {"kind", "value"} and v["kind"] in V.KINDS
            and ((v["kind"] in ("integer", "decimal") and isinstance(v["value"], str))
                 or (v["kind"] == "string" and isinstance(v["value"], str))
                 or (v["kind"] == "boolean" and isinstance(v["value"], bool))
                 or (v["kind"] == "null" and v["value"] is None)))


def _numeric_ok(v) -> bool:
    if not (_valid_typed(v) and V.is_numeric(v)):
        return False
    try:
        V.to_decimal(v)
        return True
    except V.LiteralError:
        return False


@dataclass
class Gathered:
    sources: list            # (artifact_id, record|None, rendition_id|None)
    observations: list       # witness-form facts
    blocked: list            # blocked key occurrences
    unusable: list           # (artifact_id, summary)
    excluded_origin: bool


def _gather(ctx: CheckContext, claim: Claim, keys: tuple[str, ...] | None = None) -> Gathered:
    wanted = set(keys) if keys is not None else {claim.subject}
    prov = ctx.provenance(claim)
    origin = prov.get("origin_source_artifact_id") if isinstance(prov, dict) else None
    g = Gathered([], [], [], [], False)
    for ref in claim.evidence_refs:
        art = ctx.bundle.artifact(ref)
        if art is None or art.media_type == PROVENANCE_MEDIA:
            continue
        if ref == origin:
            g.excluded_origin = True
            continue
        rec, rend = ctx.extraction_for(ref)
        g.sources.append((ref, rec, rend))
        if rec is None:
            g.unusable.append({"artifact_id": ref, "coverage": "NO_EXTRACTION_RECORD"})
            continue
        summary = rec["coverage"]["summary"]
        if summary not in USABLE:
            g.unusable.append({"artifact_id": ref, "coverage": summary})
            continue
        if summary == "PARTIALLY_PARSED":
            pass
        for f in rec["facts"]:
            if f["key"] in wanted:
                loc = f["locator"]
                g.observations.append({
                    "artifact_id": ref, "rendition_artifact_id": rend, "key": f["key"],
                    "value": f["value"], "grammar": f["grammar"], "byte_start": loc["byte_start"],
                    "byte_end": loc["byte_end"], "context_start": loc.get("context_start"),
                    "context_end": loc.get("context_end"), "token_digest": f["token_digest"],
                    "span_id": f["span_id"], "coverage": summary, "qualifiers": f.get("qualifiers", [])})
        for b in rec.get("blocked_keys", []):
            if b["key"] in wanted:
                g.blocked.append({"artifact_id": ref, "reason": b["reason"], "occurrences": b["occurrences"]})
    g.observations.sort(key=lambda o: (o["artifact_id"], o["byte_start"]))
    return g


def _finding(claim: Claim, verdict: Verdict, rule: "BaseRule", reason: str, g: Gathered | None,
             witness=None) -> Finding:
    refs = [s[0] for s in g.sources] if g else []
    if witness is not None and len(canonical_bytes(witness)) > MAX_WITNESS_BYTES:
        witness = {"truncated": True}
    return Finding.make(claim.id, verdict, rule.rule_id, rule.rule_version, refs,
                        claim.assumption_refs, reason, witness)


class BaseRule:
    rule_id: str
    rule_version = "1"
    predicate: str

    def _applicability(self, claim: Claim):
        args = claim.arguments
        ap = args.get("applicability") if isinstance(args, dict) else None
        if ap is None:
            return None
        if (isinstance(ap, dict) and ap.get("decision") == "NOT_APPLICABLE"
                and isinstance(ap.get("rationale"), str) and ap["rationale"].strip()
                and isinstance(ap.get("scope"), str) and ap["scope"].strip()):
            return Finding.make(claim.id, Verdict.NOT_APPLICABLE, self.rule_id, self.rule_version,
                                [], claim.assumption_refs, "reviewer_applicability_decision",
                                {"rationale": ap["rationale"], "scope": ap["scope"]})
        if isinstance(ap, dict) and ap.get("decision") == "APPLICABLE":
            return None
        return False  # malformed

    def invalid(self, claim: Claim, why: str) -> Finding:
        return Finding.make(claim.id, Verdict.INVALID_EVIDENCE, self.rule_id, self.rule_version,
                            [], claim.assumption_refs, "malformed_claim_arguments", {"detail": why})

    def evaluate(self, ctx: CheckContext, claim: Claim) -> Finding:
        ap = self._applicability(claim)
        if ap is False:
            return self.invalid(claim, "applicability must be {decision, rationale, scope}")
        if isinstance(ap, Finding):
            return ap
        if not isinstance(claim.arguments, dict):
            return self.invalid(claim, "arguments must be an object")
        err = self.validate_args(claim.arguments)
        if err:
            return self.invalid(claim, err)
        g = _gather(ctx, claim, self.keys(claim))
        return self.decide(claim, g)

    def keys(self, claim: Claim) -> tuple[str, ...] | None:
        return None                      # default: the claim subject

    def validate_args(self, args) -> str | None:  # pragma: no cover - overridden
        return None

    def decide(self, claim: Claim, g: Gathered) -> Finding:  # pragma: no cover - overridden
        raise NotImplementedError

    # shared early exits ------------------------------------------------------------------
    def no_observation(self, claim: Claim, g: Gathered) -> Finding | None:
        if not g.sources:
            return _finding(claim, Verdict.INSUFFICIENT_EVIDENCE, self,
                            "no_independent_evidence", g,
                            {"excluded_assertion_source": g.excluded_origin})
        if g.blocked and not g.observations:
            return _finding(claim, Verdict.HUMAN_REVIEW_REQUIRED, self, "evidence_key_ambiguous", g,
                            {"blocked": g.blocked})
        if not g.observations:
            only_corrupt = g.unusable and all(u["coverage"] == "CORRUPT" for u in g.unusable) \
                and len(g.unusable) == len(g.sources)
            verdict = Verdict.INVALID_EVIDENCE if only_corrupt else Verdict.INSUFFICIENT_EVIDENCE
            return _finding(claim, verdict, self,
                            "evidence_unreadable" if only_corrupt else "fact_not_found", g,
                            {"searched": [{"artifact_id": s[0],
                                           "coverage": (s[1]["coverage"]["summary"] if s[1] else "NO_EXTRACTION_RECORD")}
                                          for s in g.sources]})
        return None


class FactEquals(BaseRule):
    rule_id, predicate = "rule.fact_equals", "fact.equals"

    def validate_args(self, args):
        if set(args) - {"expected", "applicability"} or not _valid_typed(args.get("expected")):
            return "arguments need a typed 'expected' value"
        return None

    def decide(self, claim, g):
        early = self.no_observation(claim, g)
        if early:
            return early
        expected = claim.arguments["expected"]
        results = [V.compare_equal(o["value"], expected) for o in g.observations]
        w = {"expected": expected, "observations": g.observations, "unusable_sources": g.unusable,
             "blocked": g.blocked}
        if any(r is None for r in results):
            return _finding(claim, Verdict.HUMAN_REVIEW_REQUIRED, self, "kind_mismatch", g, w)
        if any(r is False for r in results) and any(r for r in results):
            return _finding(claim, Verdict.HUMAN_REVIEW_REQUIRED, self, "evidence_conflict", g, w)
        if any(r is False for r in results):
            return _finding(claim, Verdict.CONTRADICTED, self, "value_mismatch", g, w)
        if g.unusable or g.blocked:
            return _finding(claim, Verdict.HUMAN_REVIEW_REQUIRED, self, "evidence_source_unreadable", g, w)
        return _finding(claim, Verdict.SUPPORTED, self, "value_equal", g, w)


class FactNumericBound(BaseRule):
    rule_id, predicate = "rule.fact_numeric_bound", "fact.numeric_bound"
    OPS = {"lt": lambda a, b: a < b, "lte": lambda a, b: a <= b,
           "gt": lambda a, b: a > b, "gte": lambda a, b: a >= b}

    def validate_args(self, args):
        if set(args) - {"op", "bound", "applicability"} or args.get("op") not in self.OPS \
                or not _numeric_ok(args.get("bound")):
            return "arguments need op in lt|lte|gt|gte and a numeric 'bound'"
        return None

    def decide(self, claim, g):
        early = self.no_observation(claim, g)
        if early:
            return early
        args = claim.arguments
        bound, op = V.to_decimal(args["bound"]), self.OPS[args["op"]]
        w = {"op": args["op"], "bound": args["bound"], "observations": g.observations,
             "unusable_sources": g.unusable, "blocked": g.blocked}
        if not all(V.is_numeric(o["value"]) for o in g.observations):
            return _finding(claim, Verdict.HUMAN_REVIEW_REQUIRED, self, "kind_mismatch", g, w)
        res = [op(V.to_decimal(o["value"]), bound) for o in g.observations]
        if all(res) and not (g.unusable or g.blocked):
            return _finding(claim, Verdict.SUPPORTED, self, "bound_satisfied", g, w)
        if not any(res):
            return _finding(claim, Verdict.CONTRADICTED, self, "bound_violated", g, w)
        return _finding(claim, Verdict.HUMAN_REVIEW_REQUIRED,
                        self, "evidence_conflict" if not all(res) else "evidence_source_unreadable", g, w)


class FactPresent(BaseRule):
    rule_id, predicate = "rule.fact_present", "fact.present"

    def validate_args(self, args):
        return "no arguments allowed besides applicability" if set(args) - {"applicability"} else None

    def decide(self, claim, g):
        early = self.no_observation(claim, g)
        if early:
            return early
        w = {"observations": g.observations, "unusable_sources": g.unusable, "blocked": g.blocked}
        return _finding(claim, Verdict.SUPPORTED, self, "fact_present", g, w)


class IntervalRelation(BaseRule):
    """Relation between two activities whose bounds are document facts `<name>.start` / `<name>.end`,
    optionally with uncertainty `<name>.start_min|start_max|end_min|end_max` (chapter 06). Facts are assumed to
    share the declared coordinate (e.g. seconds since one clock); the claim states it so it stays visible."""
    rule_id, predicate = "rule.interval_relation", "interval.relation"
    RELATIONS = ("precedes", "no_overlap", "overlaps")
    SUFFIXES = ("start", "end", "start_min", "start_max", "end_min", "end_max")

    def validate_args(self, args):
        ok = (not set(args) - {"left", "right", "relation", "coord", "applicability"}
              and all(isinstance(args.get(k), str) and args[k].strip() for k in ("left", "right", "coord"))
              and args.get("left") != args.get("right") and args.get("relation") in self.RELATIONS)
        return None if ok else "arguments need distinct left/right names, a relation and a coordinate"

    def keys(self, claim):
        a = claim.arguments
        return tuple(f"{n}.{x}" for n in (a["left"], a["right"]) for x in self.SUFFIXES)

    def decide(self, claim, g):
        from decimal import Decimal
        from . import temporal as T
        a = claim.arguments
        if not g.sources:
            return _finding(claim, Verdict.INSUFFICIENT_EVIDENCE, self, "no_independent_evidence", g,
                            {"excluded_assertion_source": g.excluded_origin})
        by_key: dict[str, list] = {}
        for o in g.observations:
            by_key.setdefault(o["key"], []).append(o)
        w = {"relation": a["relation"], "coord": a["coord"], "observations": g.observations,
             "unusable_sources": g.unusable, "blocked": g.blocked}
        if g.blocked:
            return _finding(claim, Verdict.HUMAN_REVIEW_REQUIRED, self, "evidence_key_ambiguous", g, w)

        def bounds(name: str, which: str):
            """(lo, hi) possible values of name.start/end, or a reason string when they cannot be established."""
            point, lo_k, hi_k = f"{name}.{which}", f"{name}.{which}_min", f"{name}.{which}_max"
            found = {k: by_key.get(k, []) for k in (point, lo_k, hi_k)}
            vals = {}
            for k, obs in found.items():
                if len({canonical_bytes(o["value"]) for o in obs}) > 1:
                    return "evidence_conflict"
                if obs:
                    if not V.is_numeric(obs[0]["value"]):
                        return "kind_mismatch"
                    vals[k] = V.to_decimal(obs[0]["value"])
            lo = vals.get(lo_k, vals.get(point))
            hi = vals.get(hi_k, vals.get(point))
            if lo is None or hi is None:
                return "bound_missing"
            if lo > hi or (point in vals and not (lo <= vals[point] <= hi)):
                return "bounds_inconsistent"
            return (lo, hi)

        parts = {}
        for name in (a["left"], a["right"]):
            for which in ("start", "end"):
                r = bounds(name, which)
                if isinstance(r, str):
                    verdict = Verdict.INSUFFICIENT_EVIDENCE if r == "bound_missing" else (
                        Verdict.INVALID_EVIDENCE if r == "bounds_inconsistent" else Verdict.HUMAN_REVIEW_REQUIRED)
                    return _finding(claim, verdict, self, r, g, {**w, "needed": f"{name}.{which}"})
                parts[(name, which)] = r
        C = a["coord"]
        mk = lambda n: T.Activity(T.Interval(*parts[(n, "start")], C), T.Interval(*parts[(n, "end")], C))
        try:
            L, R = mk(a["left"]), mk(a["right"])
        except ValueError:
            return _finding(claim, Verdict.INVALID_EVIDENCE, self, "bounds_inconsistent", g, w)
        w["bounds"] = {f"{n}.{x}": T.Interval(*parts[(n, x)], C).to_dict()
                       for n in (a["left"], a["right"]) for x in ("start", "end")}
        rel = a["relation"]
        if rel == "precedes":
            res = (T.Tri.YES if L.end.hi <= R.start.lo else T.Tri.NO if L.end.lo > R.start.hi else T.Tri.UNKNOWN)
        else:
            ov = T.positive_overlap(L, R)
            res = ov if rel == "overlaps" else {T.Tri.YES: T.Tri.NO, T.Tri.NO: T.Tri.YES, T.Tri.UNKNOWN: T.Tri.UNKNOWN}[ov]
        if res == T.Tri.UNKNOWN:
            w["needed"] = "tighter bounds or an explicit ordering/synchronisation assumption"
            return _finding(claim, Verdict.INSUFFICIENT_EVIDENCE, self, "relation_uncertain", g, w)
        if res == T.Tri.YES:
            if g.unusable:
                return _finding(claim, Verdict.HUMAN_REVIEW_REQUIRED, self, "evidence_source_unreadable", g, w)
            return _finding(claim, Verdict.SUPPORTED, self, "relation_holds", g, w)
        return _finding(claim, Verdict.CONTRADICTED, self, "relation_violated", g, w)


class RuleRegistry:
    def __init__(self):
        self._rules: dict[tuple[str, str], BaseRule] = {}

    def register(self, rule: BaseRule) -> None:
        key = (rule.rule_id, rule.rule_version)
        if key in self._rules:
            raise ValueError(f"duplicate rule {key}")
        self._rules[key] = rule

    def get(self, rule_id: str, version: str) -> BaseRule | None:
        return self._rules.get((rule_id, version))

    def keys(self):
        return sorted(self._rules)

    def implemented_predicates(self) -> dict[tuple[str, str], str]:
        return {k: r.predicate for k, r in self._rules.items()}

    def default_plan(self) -> dict[str, tuple[str, str]]:
        return {r.predicate: (r.rule_id, r.rule_version) for r in self._rules.values()}

    def profile_digest(self) -> str:
        table = [[r.predicate, r.rule_id, r.rule_version] for r in self._rules.values()]
        return sha256_digest(canonical_bytes(sorted(table)))


def builtin_registry() -> RuleRegistry:
    reg = RuleRegistry()
    for rule in (FactEquals(), FactNumericBound(), FactPresent(), IntervalRelation()):
        reg.register(rule)
    return reg


PROFILE_ID, PROFILE_VERSION = "verifier-lab.builtin-rules", "1"

"""Deterministic dispatch: stable ordering, validated findings, errors kept separate (chapter 02)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .canonical import canonical_bytes
from .model import Claim, ExecutionError, Finding, Verdict
from .rules import MAX_WITNESS_BYTES, CheckContext, RuleRegistry

UNSUPPORTED_RULE = ("dispatch.unsupported", "1")


@dataclass(frozen=True)
class CheckPlan:
    predicate_rules: tuple[tuple[str, str, str], ...]   # (predicate, rule_id, rule_version)

    @staticmethod
    def from_registry(reg: RuleRegistry) -> "CheckPlan":
        return CheckPlan(tuple(sorted((p, r, v) for p, (r, v) in reg.default_plan().items())))

    def lookup(self, predicate: str):
        return [(r, v) for p, r, v in self.predicate_rules if p == predicate]


@dataclass(frozen=True)
class CheckRun:
    findings: tuple[Finding, ...]
    errors: tuple[tuple[str, ExecutionError], ...]   # (claim_id, error)
    cancelled: bool
    completed_claims: int
    total_claims: int


def validate_finding(ctx: CheckContext, claim: Claim, rule_id: str, rule_version: str,
                     finding: Finding) -> str | None:
    if finding.claim_id != claim.id:
        return "finding_claim_mismatch"
    if (finding.rule_id, finding.rule_version) != (rule_id, rule_version):
        return "finding_rule_mismatch"
    if not isinstance(finding.verdict, Verdict):
        return "bad_verdict"
    for ref in finding.evidence_refs:
        if ref not in claim.evidence_refs:
            return "finding_unknown_evidence"
    if not set(finding.assumption_refs) <= set(claim.assumption_refs):
        return "finding_unknown_assumption"
    if finding.witness_json is not None and len(finding.witness_json.encode()) > MAX_WITNESS_BYTES:
        return "witness_too_large"
    if finding.id != finding.expected_id():
        return "finding_id_mismatch"
    return None


def check_claims(ctx: CheckContext, claims: tuple[Claim, ...], plan: CheckPlan, registry: RuleRegistry,
                 should_cancel: Callable[[], bool] = lambda: False,
                 on_progress: Callable[[int, int, Claim], None] = lambda done, total, c: None) -> CheckRun:
    findings: list[Finding] = []
    errors: list[tuple[str, ExecutionError]] = []
    ordered = sorted(claims, key=lambda c: c.id)
    cancelled = False
    done = 0
    for claim in ordered:
        if should_cancel():
            cancelled = True
            break
        matches = plan.lookup(claim.predicate)
        if not matches:
            findings.append(Finding.make(
                claim.id, Verdict.INSUFFICIENT_EVIDENCE, *UNSUPPORTED_RULE, [], claim.assumption_refs,
                "no_registered_rule", {"predicate": claim.predicate}))
        elif len(matches) > 1:
            errors.append((claim.id, ExecutionError("ambiguous_rule_selection", claim.predicate,
                                                    "multiple rules match; explicit selection required")))
        else:
            rule_id, version = matches[0]
            rule = registry.get(rule_id, version)
            if rule is None:
                errors.append((claim.id, ExecutionError("rule_not_installed", f"{rule_id}@{version}",
                                                        "planned rule is missing from the registry")))
            else:
                try:
                    finding = rule.evaluate(ctx, claim)
                    problem = validate_finding(ctx, claim, rule_id, version, finding)
                    if problem:
                        errors.append((claim.id, ExecutionError(problem, f"{rule_id}@{version}",
                                                                "rule returned an invalid finding")))
                    else:
                        findings.append(finding)
                except Exception as exc:  # rule crash is an execution error, not a verdict
                    errors.append((claim.id, ExecutionError("rule_exception", f"{rule_id}@{version}",
                                                            f"{type(exc).__name__}: {exc}")))
        done += 1
        on_progress(done, len(ordered), claim)
    findings.sort(key=lambda f: (f.claim_id, f.rule_id, f.rule_version))
    return CheckRun(tuple(findings), tuple(errors), cancelled, done, len(ordered))

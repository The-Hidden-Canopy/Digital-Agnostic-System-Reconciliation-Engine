# 10. Independent checking and campaign-report verification

## Status and purpose

This chapter proposes an independent checking path. It is not a claim that a second implementation exists. The primary verifier should emit findings with enough supporting material for a smaller checker to validate their construction. Campaign reports should also be checkable independently: editing a passing count or deleting an inconvenient test must not produce a valid report.

Independence is a property to demonstrate, not a directory name. If the checker calls the same rule evaluator as the producer, it merely repeats the same conclusion. A useful first boundary shares a written format specification and fixed vectors, but independently reconstructs the checks relevant to a witness. It can share a well-reviewed cryptographic library without sharing the proof-producing logic.

## Small initial checker, honest expansion

The first checker can be a small offline command in the same distribution with a deliberately narrow witness vocabulary. It does not require a second service or a second language. Architecture tests keep it independent of producer evaluators. Additional witness families, a separately distributed checker and cross-language implementations are later extensions. The ordinary UI shows clear findings; detailed proof receipts and campaign accounting appear only when requested. A narrow checked result is preferable to a broad claim of independence that has not been implemented.


## Three different checks

Separate report integrity, witness validity and claim validity. Report integrity asks whether the bytes match an expected digest or trusted signature. Witness validity asks whether a cited path, interval or conflicting pair really has the stated property. Claim validity asks whether the witness satisfies the applicable rule under the declared assumptions. A correctly hashed report can contain a false conclusion.

```python
# verifier_lab/checker.py
@dataclass(frozen=True)
class CheckedFinding:
    finding_id: str
    accepted: bool
    reason_code: str
    checked_rule_version: str

def check_finding(bundle: AdmittedBundle, finding: Finding,
                  rule_catalog: CheckerCatalog,
                  context: CheckContext) -> CheckedFinding: ...

def check_report(bundle: AdmittedBundle,
                 report: VerificationReport,
                 context: CheckContext) -> ReportCheck: ...
```

The checker result is meta-verification, not another domain Verdict. “Checker rejected this finding” does not automatically mean the original claim is CONTRADICTED. It means the supplied proof/report was not accepted. Operational failures remain ExecutionError records.

## Witness-oriented algorithms

For a graph-cycle finding, the witness is an ordered sequence of activity IDs. The checker verifies that each ID exists, every adjacent edge exists in the admitted graph, and the last returns to the first. It does not need to rerun the producer's graph-search algorithm. For a dependency-path claim, it checks the ordered edge sequence and endpoint identities.

For a temporal contradiction, the witness identifies the exact interval records and comparison rule. The checker parses the bounds independently and confirms the claimed inequality. If intervals overlap such that order is indeterminate, it rejects a witness claiming a proven strict order. A HUMAN_REVIEW_REQUIRED result may cite an unresolved policy choice, but cannot be laundered into a machine-checked factual conclusion.

For a content mismatch, the checker recomputes the digest from the admitted bytes. For a binding contradiction, it compares the specific referenced fields under the rule's declared normalization semantics. For INSUFFICIENT_EVIDENCE, the checker validates the missing requirement against the relevant catalog and inventory. It cannot prove global absence from a deliberately incomplete package without an external completeness commitment.

SUPPORTED findings deserve special care. One successful local check does not establish a conjunction of all required obligations. The checker must enumerate the required obligations from the exact rule version, verify each, and reject unrecognized or omitted obligations. Introduce witness checking one rule family at a time, with an explicit unsupported result for uncovered families.

## Avoid correlated implementation errors

Place the initial checker under a separate module and forbid imports of producer evaluators in an architecture test. Use an alternative algorithm where practical: producer topological sorting versus checker explicit cycle-path checking; producer interval sweep versus checker pairwise inequalities on the supplied witness. Compare both on manually reviewed tiny examples.

A later independent-language implementation can strengthen the boundary, but language diversity alone is insufficient if both copy the same mistake. The written specification, oracle cases and disagreement triage matter more. Never vote away a disagreement. Preserve both outputs, classify whether it is a specification ambiguity, malformed witness, producer defect or checker defect, and prevent a release from describing the disputed case as supported.

## Campaign manifest and report

Use a fresh campaign manifest containing a format version, expected case IDs, case digests, expected outcomes, generator identity, rule-catalog digest and execution configuration digest. A report references that manifest and records each attempted case, actual outcome, comparison result, duration as diagnostic data, and any ExecutionError.

```python
# verifier_lab/campaign.py
def verify_campaign_report(manifest, report, case_store):
    require_exact_case_set(manifest.expected_cases, report.cases)
    require_unique_ids(report.cases)
    for case in report.cases:
        require_digest(case_store[case.id], manifest.digest_for(case.id))
        require_expected_matches_manifest(case, manifest)
        require_case_comparison_consistent(case)
    require_summary_recomputed_from_cases(report)
    require_report_digest(report)
    return CampaignReportCheck(accepted=True)
```

A digest on an untrusted manifest does not establish that it is the intended campaign. The expected manifest digest must come from the caller, release configuration or another trusted channel. Without that anchor, a malicious actor can remove a test and recompute both manifest and report consistently. The checker can still establish internal consistency, but must label the narrower conclusion.

## Error and result accounting

Separate passed cases, failed expectation comparisons, skipped preconditions, unsupported checks and execution errors. Do not include skipped or errored cases in a passing denominator. A run can be deterministic yet incomplete. A report can be internally valid while documenting failing cases. Preserve these distinctions in both JSON and human output.

Canonical normative report identity excludes wall-clock durations, host paths and other diagnostics. It includes the bundle, catalog, context, findings and case identities. Keep signatures detached or otherwise exclude their own bytes from the material they sign. Any redacted report is a new artifact with its own identity, not a silently edited original.

## Acceptance tests

Test forged passing totals, duplicate case IDs, omitted expected cases, invented case IDs, altered expected outcomes, changed build identity, unknown witness versions, nonexistent witness edges and validly hashed but semantically false reports. Test the harder case where an attacker recomputes a self-consistent report: the trusted campaign manifest must still detect the omitted case.

Require an intentional producer bug to be caught by the checker, and an intentional checker bug to be caught by fixed oracle cases. Run report verification without importing producer modules and without network access. Until those checks are actually run, “independent checker” is a design milestone, not a delivered property.

## Pattern provenance

[SIDECHANNEL's campaign verifier](https://github.com/The-Hidden-Canopy/SIDECHANNEL/blob/191f313303674b643b2aa45a5c838a44140f1308/src/verification/faults.mjs) was inspected for case-count reconciliation and report digest checking. The stronger trusted-manifest and independent-witness design above is proposed extrapolation. It does not reproduce that project's schema or claim its implementation provides every property described here.

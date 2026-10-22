# 09. Mutation generation and metamorphic testing

## Status and purpose

This is a proposed test-system design. A deterministic verifier can consistently produce the wrong answer. Running it twice is useful, but it is not a correctness oracle. The laboratory therefore needs three complementary mechanisms: small cases with manually specified expected findings, targeted mutations whose semantic effect is known, and metamorphic relations describing changes that should preserve or predictably alter a result.

Fresh inspection of panic-button found deterministic replay inputs, logical-clock behavior, stable same-time ordering and malformed-graph tests. SIDECHANNEL contains a bounded fault campaign with individual case results and consistency checks. The new laboratory generalizes these practices without copying scenarios, payloads, limits or product vocabulary.

## Keep the first harness small

Start with fixed synthetic cases, a handful of semantic mutations and two or three justified metamorphic relations in the ordinary test suite. The production UI does not need to expose generator configuration. Large campaigns, automated shrinking and user-authored relation catalogs are optional advanced modes added after the core is trustworthy. Use functions and explicit registries inside the same repository, not a separate testing service or plugin marketplace. The depth described here is a growth path, not a requirement to build every generator before the first usable application.


## Test data is newly invented

Use neutral actors, resources and activities such as actor-A, object-B and operation-C. Synthetic generators must never sample production logs, private repositories, internal policies, or customer records. A generator's inputs are a seed, an explicit generic construction recipe and a size budget. Seed reproducibility alone is insufficient: pin the generator version and algorithm as well.

```python
# verifier_lab/mutation.py
@dataclass(frozen=True)
class MutationSpec:
    mutation_id: str
    target_ref: str
    operator: str
    parameters: Mapping[str, JsonValue]
    expected_relation: str

@dataclass(frozen=True)
class MutationRecord:
    base_digest: str
    result_digest: str
    generator_version: str
    seed: str
    applied: MutationSpec

def mutate(bundle: Bundle, spec: MutationSpec) -> MutatedBundle: ...

# verifier_lab/metamorphic.py
def compare_relation(base: VerificationReport,
                     changed: VerificationReport,
                     relation: RelationSpec) -> RelationResult: ...
```

Mutation operators live in the installed test harness. A bundle may describe data to mutate, but may not provide Python source, import paths or shell commands. Every mutation produces a fresh bundle and a provenance record; the admitted original remains unchanged.

## Two mutation layers

Byte-level corruption deliberately damages serialization, framing or digests. It tests the ingestion boundary and should ordinarily yield INVALID_EVIDENCE or an explicit operational resource-limit error. Examples include duplicate keys, truncated records, malformed UTF-8 and changed artifact bytes.

Semantic mutation starts from a valid package and deliberately reconstructs all identities and references required by the format. It tests the actual rule, not merely whether the hash checker notices editing. For example, change the generic actor referenced by an authorization claim, rebuild dependent IDs and content hashes, then expect the identity-binding rule to produce CONTRADICTED. If the operator leaves a stale digest, it has tested integrity, not actor binding.

This distinction must appear in case metadata and reports. A campaign with hundreds of hash failures is not evidence of hundreds of independently exercised semantic invariants.

## Operator families

Define narrowly scoped operators: remove required evidence; replace a referenced actor; move an activity outside an interval; reverse one dependency edge; introduce a cycle; duplicate an execution identifier; change a dependency generation; alter a terminal result; remove a declared trust assumption; and create two conflicting claims about the same synthetic subject.

Each operator declares preconditions. A temporal-expiry mutation requires an applicable time-sensitive claim and precise enough time evidence. If the precondition fails, report CASE_NOT_APPLICABLE in the campaign execution layer, not a passing test. That campaign status is not the domain verdict NOT_APPLICABLE; keep the namespaces separate.

```python
def execute_case(case, kernel):
    if not case.preconditions_hold():
        return CaseResult.skipped("mutation_precondition")
    variant = case.build_variant()
    actual = kernel.verify(variant.bundle, case.context)
    # Expectations are committed test data, never derived from actual.
    return compare_expected(case.expected, actual,
                            mutation=variant.record)
```

Do not assume one mutation changes exactly one finding. Removing shared evidence may affect several claims. The expectation can specify a required changed finding, an allowed set of collateral effects and protected findings that must remain unchanged. This produces a more useful oracle than comparing only overall status.

## Metamorphic relations

Start with a small, explicitly justified relation catalog:

1. Reordering JSON object keys preserves canonical identity and findings.
2. Reordering members of an explicitly unordered set preserves findings; reordering a causal event sequence does not receive this guarantee.
3. Bijectively renaming opaque identifiers while rebuilding references preserves semantic findings after normalizing those identifiers.
4. Adding a disconnected, irrelevant artifact does not strengthen support for an unrelated claim.
5. Removing the only required witness cannot leave that claim SUPPORTED under the same rule and assumptions.
6. Increasing uncertainty must not create a stronger temporal conclusion unless the rule explicitly defines a non-monotonic relation.
7. Running with different wall-clock times preserves findings when logical time and trust snapshot are fixed.
8. Changing a relevant dependency version invalidates a stale derived result; changing an unrelated version does not.

“More evidence never hurts” is not a valid general law: new evidence can contradict earlier evidence. Likewise, arbitrary event permutation is not a valid law. Encode relation preconditions and explain why each relation is sound for the tested rule.

## Bounded generation, shrinking and reproducibility

A standard-library reference harness can use `unittest` plus a small deterministic pseudo-random generator from `random.Random`. This is a testing convenience, not cryptography. Record exact generated bundles because future Python releases or generator changes can affect reproduction. Prefer an explicitly documented deterministic generation algorithm for cross-language campaigns.

Generate valid bases before malformed variants. Bound graph size, branching, depth, artifact bytes and cases per run. On failure, invoke Chapter 07's counterexample minimizer, preserving both the rule's failing property and structural validity. A minimized case is an explanatory artifact, not proof that all smaller cases were explored. Retain original and minimized cases with their digests.

## Acceptance and coverage

Acceptance requires at least one positive, negative and missing-evidence case per rule; every registered mutation operator must run against a compatible base; every metamorphic relation needs both a satisfying example and an example where its precondition fails. Test that the harness detects an intentionally defective verifier, such as one that always returns SUPPORTED. An all-green campaign against that defective verifier is a harness failure.

Track rule coverage, operator coverage, precondition coverage and execution failures separately. Publish surviving mutations and skipped cases rather than hiding them. Report a mutation-detection ratio only with its denominator and scope; it is not a universal safety score.

## Pattern provenance

- [panic-button malformed-input and deterministic replay tests](https://github.com/The-Hidden-Canopy/panic-button/blob/b40094e6b412a68dc72da28f189af33a6bf4e0eb/src/lib/deterministicRuntime.fuzz.test.ts).
- [SIDECHANNEL fault campaign](https://github.com/The-Hidden-Canopy/SIDECHANNEL/blob/191f313303674b643b2aa45a5c838a44140f1308/src/verification/faults.mjs).

The former was inspected as public MIT code; the latter's public visibility was verified but a release license was not. This chapter proposes fresh implementation in either case, not copying.

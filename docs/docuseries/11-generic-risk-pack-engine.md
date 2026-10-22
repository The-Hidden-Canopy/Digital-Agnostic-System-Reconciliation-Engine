# 11. A generic risk-pack engine

## Status and purpose

This is a proposed implementation. A risk pack is a versioned, declarative mapping from a stated context to verification obligations and evidence requirements. It is not an executable plugin, a universal compliance certificate, or a copy of an internal policy. The engine must work without knowing a vendor, industry or jurisdiction. Chapters 14–15 can add carefully sourced jurisdiction-specific content without changing the generic mechanism.

The central separation is between a risk description, an applicability decision, an obligation, the evidence needed to test it, and the resulting finding. A document mentioning a risk does not establish that the risk applies. An applicable obligation with no evidence is not satisfied merely because no violation was observed.

## Minimal initial surface

The application must work with a small built-in generic check set and no risk packs selected. Ship one optional synthetic example pack to exercise this interface; do not make a pack marketplace, jurisdiction catalog, remote registry or policy-authoring language prerequisites. The initial applicability grammar is intentionally small. Users follow import → review claims → run checks → findings → export, with applicability details and pack selection progressively disclosed. All catalogs are local data in the same application. New packs extend coverage without restructuring the kernel.


## Data model and boundaries

The following names are new proposed records, not inherited schemas:

```python
# verifier_lab/riskpacks.py
@dataclass(frozen=True)
class RiskPack:
    pack_id: str
    version: str
    content_digest: str
    compatible_bundle_versions: tuple[str, ...]
    applicability: PredicateSpec
    obligations: tuple[ObligationSpec, ...]
    source_refs: tuple[SourceRef, ...]

@dataclass(frozen=True)
class ObligationSpec:
    obligation_id: str
    claim_predicate: str
    rule_ref: RuleRef
    evidence_requirements: tuple[EvidenceRequirement, ...]
    review_requirements: tuple[str, ...]

def resolve_pack(reference: ProfileRef, catalog: PackCatalog) -> RiskPack: ...
def evaluate_applicability(pack: RiskPack,
                           facts: ContextFacts) -> ApplicabilityResult: ...
def plan_obligations(pack: RiskPack, bundle: Bundle,
                     facts: ContextFacts) -> ObligationPlan: ...
```

The common Bundle's `profile_refs` pins exact pack identity and digest. A human-readable version alone is insufficient because a publisher might replace content at the same version. Conflicting content under an existing identity is rejected. The catalog is installed and approved separately from evidence admission. A bundle cannot make a new rule executable by naming a Python module or embedding a script.

Pack references resolve only to rules in the installed CheckRule catalog. Extension code is ordinary software installation subject to explicit review and release controls; it is never loaded from an untrusted package during verification. In the first release, keep rule implementations in the core distribution and risk packs data-only.

## Applicability without false exemptions

Use a bounded predicate language with explicit types and no `eval`. Initial operators can be equality, membership in a finite set, existence, conjunction, disjunction and negation. Avoid arbitrary regex or expression evaluation in the first version. Record the exact context facts used by the decision.

Evaluate applicability with TRUE, FALSE or UNKNOWN. UNKNOWN is not FALSE. For example, a hypothetical pack applies only when an operation changes persistent state. If the supplied context does not say whether the operation mutates state, the engine cannot declare NOT_APPLICABLE. It emits an unresolved applicability requirement, ordinarily INSUFFICIENT_EVIDENCE, or HUMAN_REVIEW_REQUIRED when the classification itself requires human judgment.

Use three-valued logic: FALSE AND UNKNOWN is FALSE; TRUE AND UNKNOWN is UNKNOWN; TRUE OR UNKNOWN is TRUE; FALSE OR UNKNOWN is UNKNOWN; NOT UNKNOWN is UNKNOWN. Add truth-table tests. A NOT_APPLICABLE finding must cite the facts and rule establishing non-applicability, rather than merely recording that no matching evidence was found.

## Evidence mapping and obligation planning

The planner maps an applicable obligation to claims and required evidence predicates. It does not invent claims from a source document without the review workflow in Chapters 16–17. Extracted ClaimCandidates remain proposals. Approved ClaimRevisions are normalized into the common Claim model with provenance back to immutable source spans.

For a generic “execution requires prior authorization” obligation, the plan can require an authorization record, an execution record, a subject binding and temporal evidence. Those are generic evidence categories, not a prescribed organizational approval policy. The pack must make configurable assumptions explicit; it must not smuggle a private role hierarchy or tolerance into the engine.

```python
def plan_one(obligation, bundle, context):
    matches = match_claims(bundle.claims, obligation.claim_predicate)
    if not matches:
        return MissingClaimRequirement(obligation.obligation_id)
    plans = []
    for claim in matches:
        required = bind_requirements(obligation, claim, context)
        plans.append(CheckPlan(claim.id, obligation.rule_ref,
                               required, context.digest))
    return plans
```

Evidence matching must use explicit identifiers and typed predicates. Similar text, matching filenames or a model's confidence are not proof that an attachment satisfies a requirement. If semantic extraction is offered, preserve it as a candidate mapping requiring review or an explicitly bounded inference rule. Never silently upgrade “document says this” into “system did this.”

## Versioning and changes

A run pins bundle digest, pack digest, rule-catalog digest, trust-context digest and relevant assumptions. Re-running against a newer pack creates a new run. The UI can compare findings, but must not overwrite historical results or present a new verdict as though it were the old result.

Pack updates need a machine-readable change summary: added or removed obligations, altered applicability, changed evidence requirements, changed rule versions and changed source references. A semantic-version label is useful for communication but does not replace this diff. A profile that changes a requirement must be reviewed even if its publisher calls the update a patch.

When two packs disagree, retain both scoped findings. Do not choose the stricter or more permissive result automatically unless an explicit, versioned resolution policy calls for that behavior. The generic engine may surface HUMAN_REVIEW_REQUIRED for unresolved conflicts. It must preserve the source obligations so the user can understand the disagreement.

## Remediation is a proposal, not an action

A finding can map to a generic remediation template: supply a missing record, resolve an identity mapping, review uncertain applicability, or re-run after a dependency changes. The engine never changes external systems to make a test pass. Suggested remediation references the failed requirement, affected claims and evidence needed to close it. A user-marked “resolved” label is workflow state, not proof; closure requires a new verified run or an explicitly documented human-review decision.

## Tests and acceptance

Test all applicability truth-table cases, unknown context, contradictory facts, pack digest mismatch, unsupported bundle version, unknown rule references, cyclic pack dependencies, duplicate obligation IDs and malicious executable-looking fields. Unknown required fields must not be silently ignored. Test exact version pinning by changing a pack after a run and confirming the old report remains reproducible.

Use a synthetic risk pack involving generic artifacts and activities, with at least one supported obligation, one contradiction, one missing witness and one genuine non-applicable obligation. Add a human-review case where a semantic classification cannot be decided from available evidence. The same engine must evaluate this pack without product-specific imports.

Release acceptance includes a clear statement that risk-pack findings are evidence assessments under declared rules, not blanket legal or safety certification. The jurisdiction chapters must supply their own source currency and review requirements. No jurisdiction-specific content was verified by this chapter.

## Pattern provenance

The dependency-version binding in [SIDECHANNEL's publication boundary](https://github.com/The-Hidden-Canopy/SIDECHANNEL/blob/191f313303674b643b2aa45a5c838a44140f1308/src/evaluation/publication.mjs) motivates pinning inputs before accepting derived results. The risk-pack language, three-valued applicability and obligation planner are new design proposals, not claims about that source.

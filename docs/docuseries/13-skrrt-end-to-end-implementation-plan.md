# 13. Skrrt's end-to-end implementation plan

## Status, scope and ownership

This is an implementation-ready proposed work sequence, not an instruction already sent to a person or an agent and not a record of completed engineering. The product is a vendor- and domain-agnostic verification laboratory with a full UI and document ingestion for PDF, JSON, Markdown and SQL. It reuses general patterns through fresh implementation. It does not extract sensitive source, internal schemas, policy thresholds, proprietary adapters or real fixtures.

The target outcome is specific: a user imports documents and evidence, reviews candidate claims and applicability, runs deterministic checks, sees counterexamples and uncertainty, proposes remediation, and exports a portable report that can be independently checked. A CLI-only kernel is an intermediate milestone, not the completed product.

## Governing constraint: simple, but expandable

Build one local modular-monolith application with a small core and stable contracts. Do not turn the chapter structure into a service topology. No mandatory cloud, account, AI dependency, plugin marketplace or remote registry belongs in the first release. The default workflow is import → review claims → run checks → findings → export; advanced capabilities are progressively disclosed.

Deliver in three increments. First, a complete narrow vertical slice supports the four input formats at documented conservative subsets, manual claim review, a few generic checks, clear findings and portable export. Second, harden the same application with adversarial ingestion tests, a narrow independent checker and release reproducibility. Third, add optional risk packs, richer mutation campaigns, signature adapters, advanced reconciliation and expanded format/rule support. Required safety boundaries are never postponed, but breadth is. A usable release must meet its declared narrow scope; it need not implement every advanced method in the series.


## Dependency graph

```text
A specification and release boundary
  -> B models/canonicalization
  -> C secure byte admission
  -> D graph/replay/temporal rule kernel
  -> E witnesses and minimization
  -> F mutation/metamorphic campaign
  -> G independent checker

B + C -> H document extraction/review contracts
B + D -> I risk-pack engine
H + I + D + E -> J full UI workflow
F + G + J -> K packaging, release evidence and review
```

Some work can proceed in parallel once shared contracts are frozen. UI prototypes may use newly invented mock data while the kernel develops, but they must not invent a competing schema. Document parser work can proceed alongside rule implementation after byte-admission and SourceDocument/source-artifact contracts are established. Jurisdiction-specific packs are optional extensions on I, with their own source review; they must not block the generic laboratory's first usable release.

## A. Freeze the claims and boundaries

Deliver a concise normative specification, threat model, supported-platform matrix, module map and release-boundary checklist. State what the verifier proves under declared assumptions and what it cannot prove: source truth, complete history without an anchor, human identity without a trusted binding, or universal legal compliance.

Acceptance: every proposed verdict has a precise meaning; operational errors are separate; unknown applicability cannot become NOT_APPLICABLE; source documents cannot execute code; no private dependency is required. Record an explicit future license-choice checkpoint. Do not select a license by inference.

## B–C. Establish stable data and hostile-input admission

Implement `model.py`, `canonical.py`, `validation.py`, `ingestion/admit.py` and the trust interface. Freeze the common Bundle, Claim, Activity and Finding contracts with golden canonical vectors. Implement duplicate-key detection, unique-reference validation, immutable staging and streaming artifact hashing. Keep signature verification optional until a maintained dependency and trust-anchor configuration are chosen.

Acceptance: equivalent normative inputs produce identical identities; changed authoritative data changes identity; unsafe paths and ambiguous JSON are rejected; limits fail explicitly; original evidence is never rewritten. Demonstrate the distinction between integrity, authenticity and completeness with small synthetic examples. This is the first point where later modules can rely on admitted bytes rather than arbitrary paths.

## D–E. Build a small complete verification kernel

Start with a small coherent set of generic graph consistency, binding and temporal checks. Add dependency-version consistency, authorization-before-execution and consequence-linkage rules as separately scoped extensions, or include them only when required by the first tutorial workflow. Each rule declares required evidence, assumptions and witness format. Time is injected and intervals preserve uncertainty. Use the structured verdict enum everywhere, not overloaded booleans.

Add witness construction and counterexample minimization after the first rules work end to end. The output should explain a failure by pointing to the smallest useful conflicting records, not merely say “invalid.” Minimized examples remain linked to the original evidence and retain their minimization assumptions.

Acceptance per rule: a manually reviewed positive case, a contradiction, missing evidence, malformed evidence and an applicable human-review case where relevant. For example, two overlapping uncertain time intervals must not yield a proven order. Replay must preserve findings across wall-clock changes when logical time and trust context are fixed.

## F–G. Prove the harness can catch defects

Implement separate byte-corruption and semantic-mutation operators. Semantic operators rebuild valid package identities so they exercise the intended rule rather than only hashing. Add explicit metamorphic relations and preconditions, fixed campaign manifests and independent report consistency checks.

Build the witness checker separately from producer evaluators. Start with graph paths/cycles, interval comparisons and digest witnesses; mark uncovered rule families unsupported. Use intentionally defective producers and checkers to validate the testing strategy.

Acceptance: an always-SUPPORTED producer fails the campaign; omitted cases and forged passing totals are detected against a trusted campaign manifest; a self-consistent but semantically false witness is rejected. Every skipped or unsupported case remains visible. No aggregate percentage substitutes for the individual evidence.

## H. Add document ingestion and reviewed claims

Implement Chapter 17's SourceDocument, SourceSpan, ClaimCandidate and ClaimRevision contracts. Secure byte admission precedes extraction. PDF, JSON, Markdown and SQL extraction use isolated, format-specific parsers and documented supported subsets. SQL ingestion never executes statements against a database. Document text is data, including any instructions it contains.

Preserve original source bytes, parser version, extraction options and exact source locations. Candidate claims and evidence mappings are not automatically approved facts. Review creates a new immutable claim revision with provenance, rather than editing away the original candidate. Contradictory documents remain visible and require reconciliation.

Acceptance: each format has clean, malformed, oversized, unsupported-feature and mixed-content cases; partial extraction is visible; no network fetch or active content execution occurs; reviewed claims can be traced back to source spans; re-extraction with a new parser creates new provenance rather than mutating history.

## I–J. Risk mapping and the full UI

Implement the minimal optional data-only risk-pack interface and three-valued applicability without building a large catalog. Deliver the Chapter 16 workflow in a simple default view with advanced details progressively disclosed: import queue, source viewer, candidate review, reconciliation, risk-to-test mapping, run lifecycle, findings/witness inspection, proposed remediation and portable export.

The UI and CLI share one application service. Jobs have explicit queued/running/cancelling/cancelled/succeeded/failed states. A completed run may contain contradictions or insufficient evidence. Changing a source, claim revision, risk pack or trust context marks dependent results stale; it does not silently reuse a previous green badge.

Acceptance: keyboard-accessible end-to-end operation for every supported format; refresh during a run; cancel and retry; repeated import; conflicting revisions; missing optional parser; export failure; and stale-result detection. Human review records a reason and scope. It cannot turn absent evidence into a machine-proven finding without that distinction remaining visible.

## K. Package and review a reproducible release candidate

Build the distribution in clean environments, run the complete suite on the declared platform matrix, verify offline use, scan packaged contents and compare clean builds. Deliver a release evidence manifest, known limitations, synthetic tutorial packages and independently checkable reports. Complete license/provenance review before any public-source decision.

Acceptance: a fresh installation can perform the full UI/document workflow without private services, checkout paths or credentials; the independent checker accepts the tutorial report and rejects its tampered variants; every required gate is tied to the exact commit and artifact. Publication requires a separate authorized decision.

## Definition of done and handoff discipline

Each task returns changed modules, test commands, actual pass/fail/not-run results, artifacts and remaining blockers. Planned checks are never described as executed. A failed optional parser does not erase successful unrelated work, but a required format cannot be silently dropped from the release claim. If a blocker needs new authority, pause only the dependent action and state exactly what is needed.

The end-to-end stopping condition is a reviewable release candidate satisfying the declared matrix, not an arbitrary number of tasks or a convincing demo. Preserve a backlog for expanded rule families, independent-language checking and additional packs without mislabeling them as delivered. This plan itself does not start Skrrt, modify a repository, contact anyone or authorize a public release.

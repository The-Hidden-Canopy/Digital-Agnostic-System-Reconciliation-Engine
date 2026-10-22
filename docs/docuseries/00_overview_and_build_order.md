# 00 Building an agnostic verification laboratory

## Outcome and scope

Build one local application with a full UI, Python 3.12 backend, library, and CLI that evaluates explicit claims about evidence and stateful computations. The default workflow stays simple: import sources, review claims, run checks, inspect findings, and export. Expandable modules can detect invalid compositions, reproduce deterministic failures, and reduce failures into independently checkable experiments. This series specifies a proposed reference implementation. Illustrative code is not a tested implementation, and the series does not establish production readiness or legal compliance.

The core has no vendor knowledge. It consumes a newly specified JSON contract and runs deliberately fictional systems: a transactional key-value store, a document transformation pipeline, and a seeded job scheduler. It must not depend on a private service, model, dataset, implementation, or product adapter. Public attribution belongs in a separate appendix, not inside normative contracts or fixtures.

The UI is a core delivery surface, not a decorative dashboard. It must expose exact source evidence, reviewed interpretation, check progress, and explainable results. Advanced experiments provide a deeper outcome: a candidate implementation accepts a claim, an independent checker rejects it, and a compact experiment demonstrates why. Ordinary document checking must not require users to configure a replay engine.

## Simple default and expandable application

Use a modular monolith: one local backend, one frontend, one workspace, and typed in-process service boundaries. Do not require a queue cluster, separate graph database, distributed worker fleet, or a plugin marketplace. SQLite can store application state; exported evidence remains portable JSON and files. The frontend calls backend services also used by the CLI, avoiding a second implementation of verification semantics.

The default workflow accepts PDF, JSON, Markdown, and SQL-backed source snapshots. Ingestion produces application-layer `SourceDocument`, `SourceSpan`, and unreviewed `ClaimCandidate` records. A user reviews a candidate into an editable `ClaimDraft`; committing a revision creates an immutable core `Claim`. Source spans and extraction provenance compile into referenced artifacts rather than extending the core bundle schema. Approval records an interpretation, not proof that a source author's statement is true.

The UI provides import, source inspection, claim review, check selection, progress, findings, and export views. Advanced panels expose assumptions, rule versions, dependency graphs, replay, mutation campaigns, and reduction only when needed. Failed extraction remains a visible review state. The frontend must never show an unreviewed machine suggestion as a verified conclusion. Chapters 16 and 17 define the detailed frontend and ingestion contracts.

## Architecture and trust boundaries

Use a package named `verifier_lab` under `src/`. Keep immutable types in `model.py`; canonical encoding in `canonical.py`; structural validation in `validation.py`; composition in `graph.py`; completeness profiles in `purposes.py`; experiments in `replay.py`; comparison in `temporal.py`; and reduction in `minimize.py`. Later chapters add package integrity, adversarial generation, executable isolation, reporting, and release gates.

The subject under test and the reference checker are different components. A subject reports observations but cannot select the laboratory's verdict or rewrite reference expectations. A rule is a pure function over a validated context and claim. A runner executes an experiment and returns observations or an operational error. The report builder formats existing findings; it does not invent additional conclusions.

```python
class CheckRule(Protocol):
    rule_id: str
    rule_version: str
    def evaluate(self, context: CheckContext, claim: Claim) -> Finding: ...

class SubjectDriver(Protocol):
    def run(self, request: ExperimentRequest) -> ExperimentResult: ...
```

Third-party Python rules execute code and therefore are not made safe by these protocols. The default registry contains reviewed built-ins only. Explicit plugin loading and out-of-process execution belong at the orchestration boundary. Importing the core must never discover plugins, access the network, or start a process.

## Verdicts and operational outcomes

The shared verdict enum is `SUPPORTED`, `CONTRADICTED`, `INSUFFICIENT_EVIDENCE`, `INVALID_EVIDENCE`, `NOT_APPLICABLE`, and `HUMAN_REVIEW_REQUIRED`. Supported means supported under the recorded assumptions, rule version, evidence, and search scope. It does not mean universally true. Contradicted requires a valid witness. Invalid evidence means supplied records violate the input contract. Missing required records usually yield insufficient evidence, not invalid evidence.

Operational failures such as timeout, subprocess crash, permission failure, or an exhausted execution budget are `ExecutionError` records. They are not additional verdicts. A rule that never ran has no finding. A rule that ran and established that its evidence is insufficient may return that verdict. Reports retain both facts instead of converting every execution failure into a claim about the subject.

## Build sequence

First ship the small document-checking path through the full UI: import a supported source, review a source-linked claim, run one deterministic check, inspect the finding, and export a reloadable evidence package. Implement PDF, JSON, Markdown, and SQL ingestion as bounded modules behind the same review contract. The deeper sequence below is staged expansion, not a requirement that every user or initial release load every subsystem.

1. Implement strict parsing, immutable types, canonical identity, and fixtures. Freeze the version-one contract before adding broad features.
2. Implement a deterministic dispatcher with one exact-equality rule. Verify that record order, hash randomization, and process scheduling do not change findings.
3. Add dependency resolution and composition obligations. Produce cycle and missing-input witnesses.
4. Add purpose profiles and coherent-generation checks. Demonstrate that the same package can satisfy inspection but not exact continuation.
5. Add synthetic runners and three-way replay experiments. Compare full reference state, not only a subject digest.
6. Add temporal uncertainty and interval-based reasoning. Make abstention executable and tested.
7. Add dependency-aware failure reduction. Preserve the identity of the original defect during shrinking.
8. Add the later chapters' packaging, mutation campaigns, independent conformance tests, resource boundaries, reporting, and release workflow.

Each step should produce a small vertical slice that can be exercised through the UI, library calls, and CLI. Replay, mutation campaigns, temporal packs, and reduction are optional advanced modules. Avoid implementing every type before any end-to-end result works.

## Initial CLI contract

Specify commands `validate`, `check`, `experiment`, `minimize`, and `verify-package`. The first accepts a bundle and reports structural diagnostics. The second evaluates selected claims under pinned rules. The third executes a named synthetic subject with a supplied seed and operation sequence. The fourth consumes an existing failure package. The fifth independently verifies persisted outputs.

Every command accepts an explicit output directory and refuses to overwrite an existing result by default. Machine output is JSON; human output is a rendering of that JSON. Console diagnostics go to stderr. Exit-code mapping is defined once in the CLI chapter; individual rule modules must not call `sys.exit`.

Configuration contains deterministic work limits such as maximum nodes, operations, and oracle evaluations. A wall-clock timeout remains a protective operational limit, not a reproducible search bound. Record both separately. Do not add current timestamps, hostnames, or random UUIDs to semantic identities.

## Independence and coverage

Implement a simple reference state machine independently from a candidate implementation. Shared wire types and general-purpose hashing are acceptable; shared business transition functions are not a meaningful differential oracle. A candidate digest and a checker that simply repeats the candidate digest cannot reveal omitted state.

Start with an independently written field inventory for each synthetic state. Add positive controls, negative controls, and intentionally defective subjects. The laboratory must detect an always-pass checker and an always-fail checker. Mutation detection is reported alongside equivalent or unresolved mutations, not as a misleading universal correctness score.

## Tests and acceptance

Use standard-library `unittest` initially. Tests live under `tests/unit`, `tests/contract`, `tests/experiments`, and `tests/adversarial`. Fixture directories contain only generated or hand-authored fictional data. Every fixture states its intended claim, expected verdict, and why that verdict follows.

The first milestone is complete when the UI imports a source, records reviewed source-linked claims, runs a deterministic check, persists a finding, and exports a package that independently reloads. The same backend must support the corresponding CLI flow. A changed input must change its identity; a presentation-only change must not. A missing rule must be visible and must not silently produce support. The optional full laboratory milestone adds all three synthetic systems, a witnessed defect in each, and a reduced reproducer verified outside the reducer process.

Do not equate completion of this plan with completion of its implementation. Keep a checklist whose entries point to actual tests and generated evidence after implementation, and leave unexecuted claims explicitly unverified.

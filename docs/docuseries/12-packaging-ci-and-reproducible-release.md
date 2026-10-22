# 12. Packaging, CI and reproducible release

## Status and completion boundary

This chapter specifies a proposed release process. No build, test suite or release artifact has been produced or verified by this document. The intended product is not complete at a command-line demo: it includes the full UI and PDF/JSON/Markdown/SQL document-ingestion workflow described in Chapters 16–17, with claim review, evidence reconciliation, risk-to-test mapping and portable reports.

Python 3.12 is the reference runtime. Keep the verification kernel standard-library-first. Optional parser, cryptography and UI dependencies must be explicit, pinned and separable; their absence must produce a clear capability result rather than a fabricated pass. A downloaded evidence package must never execute code as part of installation or verification.

## One local application

Use a modular monolith: one local application process, one local store, and a small stable kernel. Parser isolation may use bounded subprocesses, but there are no mandatory microservices, cloud services, accounts, AI models or remote registries. Keep the default UI to import → review claims → run checks → findings → export. Advanced risk packs, trust anchors, campaign views and proof details are optional surfaces. Package a complete narrow workflow first, then grow rule and parser coverage through versioned interfaces. The full UI requirement means a coherent local workflow, not an enterprise administration platform.


## Repository and distribution layout

```text
pyproject.toml
src/verifier_lab/
  model.py canonical.py validation.py graph.py purposes.py
  replay.py temporal.py minimize.py trust.py
  ingestion/{admit,limits,worker,coverage}.py
  ingestion/adapters/{pdf,json,markdown,sql}.py
  mutation.py metamorphic.py checker.py campaign.py riskpacks.py
  cli.py
tests/
  unit/ contract/ adversarial/ differential/ ui/
  synthetic/ golden/
apps/workbench/
docs/
profiles/
```

The build backend is a development dependency, not automatically a runtime dependency. The shipped wheel should contain the core, schemas/catalog data and necessary UI assets, but no private checkout paths, logs, credentials, copied source fixtures or proprietary adapters. Add a package-content allowlist and a release scan. Generated artifacts are checked against that allowlist before signing or publication.

Before choosing a public license, complete an explicit owner decision and provenance review. Public visibility of a reference repository is not a license grant. Pattern reuse does not justify copying implementation text. Preserve attribution where actual licensed material is intentionally incorporated, and record dependency licenses separately. The release process must pause at the license-choice checkpoint rather than assume one.

## One kernel, multiple surfaces

The CLI and UI call the same application service around the verification kernel. UI convenience must not introduce a second rule engine. Command results are stable JSON records; human rendering is a separate projection.

```text
verifier-lab inspect package/
verifier-lab verify package/ --context context.json --output report.json
verifier-lab check-report package/ report.json
verifier-lab campaign campaign.json --output campaign-report.json
verifier-lab serve --host 127.0.0.1
```

These commands are proposed, not installed. The exact exit-code contract should be frozen with tests. A useful starting distinction is: successful completion of verification; completed verification with findings requiring attention; invalid input; and operational failure. Never conflate a non-SUPPORTED domain verdict with a crashed verifier. The JSON output remains authoritative when multiple verdicts occur.

The local server binds to loopback by default, with an explicit origin/CSRF defense and no unauthenticated remote binding. Opening local UI functionality is not permission to expose private evidence to a network. File imports and report downloads use safe server-generated identifiers, not arbitrary filesystem paths supplied by the browser. See Chapter 16 for lifecycle, accessibility and job-state requirements.

## Reproducibility has separate layers

Semantic reproducibility means identical normative inputs yield identical findings and report identities. Diagnostic timestamps, durations and machine paths are excluded from normative identity. Build reproducibility means the same source and declared toolchain produce matching distribution artifacts. These are different properties and must be reported separately.

Pin build dependencies with hashes, document interpreter and operating-system assumptions, set a deterministic build timestamp where the backend supports it, normalize archive ordering and permissions, and compare two clean builds in separate directories. If a platform-specific artifact cannot yet be byte-reproducible, state that limitation rather than claiming the semantic replay test covers it.

Golden vectors cover canonical bytes and digests across platforms. Unicode normalization, integer bounds, timestamp representations and sorting order must be specified. Avoid locale-sensitive ordering in normative operations. SQL extraction must not depend on database row order unless an explicit order is present. Parser version changes create new extraction provenance rather than silently changing an existing claim set.

## CI gate sequence

1. Static checks: syntax compilation, forbidden imports, package-data allowlist and deterministic generated-file checks.
2. Unit and contract tests: canonicalization, models, rule semantics, trust boundaries, applicability logic and stable error codes.
3. Adversarial ingestion tests: hostile paths, malformed files, parser budgets, unsupported constructs and no source mutation.
4. Property and mutation campaigns: fixed seeds, exact case manifests, surviving mutations and skipped cases visible.
5. Independent checker tests: verify producer reports without producer evaluator imports; reject forged summaries and witnesses.
6. UI/integration tests: import each supported format, review candidates, reconcile contradictions, map risks, run tests, inspect witnesses and export a portable report.
7. Packaging tests: install the built wheel in a clean environment, run offline examples and confirm no source checkout or hidden service is required.
8. Reproducibility comparison: two clean builds and cross-platform semantic vectors.

Do not let a focused pass become a full-release claim. Store results for the exact source commit, build artifact and test manifest. A rerun on a modified tree is new evidence.

## Hostile-document and UI release gates

PDF parsing runs in the isolated extraction boundary, with resource limits and no active content execution. JSON parsing rejects ambiguous structures. Markdown renders as escaped/sanitized content, never executable HTML or automatic external fetches. SQL is parsed as text or constrained schema evidence; never connect to a user database or execute statements merely because a file was imported. Chapter 17 owns the exact parser contracts and supported subsets.

The UI must represent partial extraction, unsupported content, cancelled runs and stale results honestly. Test cancel/retry, repeated clicks, refresh during work, missing optional parsers, report export failure and import of the same source twice. A job marked SUCCEEDED means the computation completed, not that all claims were SUPPORTED. Accessibility and keyboard navigation are release criteria, not optional polish.

## Release evidence and stopping condition

Produce an artifact manifest, source commit, dependency inventory, license decision, test/campaign manifests, build-reproducibility result, known limitations and portable verification examples. Optional signatures use an authorized release key; this design does not authorize creating credentials or publishing anything.

A release candidate is complete when all required gates for its declared platform/format matrix are green, unresolved limitations are visible, and the full UI/document workflow works from the distributed artifact. Publication, repository visibility changes and signing-key operations are separate authorized actions. “Ready for review” must not be reported as “released.”

## Pattern provenance

The portable, independently usable direction is consistent with the public [Open Canopy Contract](https://github.com/The-Hidden-Canopy/HAVEN/blob/182ddd27f577c20f43c658f0725302560a118dd7/OPEN_CANOPY_CONTRACT.md). This chapter proposes its own concrete build and release gates; it does not assert they have already been met.

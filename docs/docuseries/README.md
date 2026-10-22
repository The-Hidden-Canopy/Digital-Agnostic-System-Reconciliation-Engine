# Agnostic Verification Laboratory: End-to-End Build Docuseries

Prepared for The Hidden Canopy and Damion “Skrrt” McClellan. Design baseline: 21 October 2026.

## What this packet is

An implementation handoff for a standalone open-source-oriented verification product, including its full UI, evidence ingestion, stateful experiments, independent checking, and EU/US/China/Australia risk-evidence packs. It extrapolates general engineering patterns into newly specified methods. It is not a code extraction, an already-built application, a legal certification, or a claim that illustrative code has passed tests.

The product name is descriptive and provisional. Public publication, final licensing and repository creation are separate release decisions. Nothing in this packet authorizes accessing a customer's systems or executing imported files.

## Non-negotiable boundaries

- Vendor- and domain-agnostic core; no Hidden Canopy product required.
- Fresh generic contracts and synthetic examples; no private source, internal schemas, policies, sensitive fixtures, telemetry or customer data.
- PDF, JSON, Markdown and SQL are the core requested document formats. SQL ingestion is parsing, not execution. OCR is a local optional extraction stage with explicit uncertainty.
- Imported assertions are candidate claims, not verified facts. Imported instructions cannot authorize operations.
- A first-class UI is required. The CLI and library share the same core, not a separate interpretation of results.
- Technical findings, evidence completeness, execution errors and human/legal determinations remain distinct.
- No mandatory AI provider, telemetry, cloud account or external service.

## Simple by default, expandable by design

The normal workflow is **Import → Review claims → Run checks → Findings → Export**. Deliver one local application around a small modular core, not a distributed service platform. Advanced graphs, replay, experiments and custom packs are progressively disclosed. Extensions attach through explicit versioned contracts; users do not need them to complete the default workflow. Deep implementation documentation must not become a requirement to understand the internals before using the product.

## Reading and building

Start with chapter 00 and the type/schema chapter. Implement the core and small synthetic systems first, alongside the UI shell and import review path. Build vertical slices from an imported source through reviewed claim, check, finding, remediation and portable report. Do not leave frontend integration until every backend module is complete.

Chapters 00–07 define the core methods. Chapters 08–13 cover secure ingestion boundaries, falsification, independent reports, generic risk packs, packaging and the delivery task graph. Chapters 14–15 give jurisdiction source baselines and implementation examples. Chapters 16–17 specify the UI and PDF/JSON/Markdown/SQL import/reconciliation pipeline.

The implementation team must reconcile contracts at each integration checkpoint. Pseudocode and proposed paths are design instructions, not assertions that modules already exist. Dependency versions and platform sandboxing must be validated when implemented.

## Release gates

1. End-to-end UI and CLI acceptance on all four document formats.
2. Positive and negative controls, independent checker disagreement handling, replay tests and reduced counterexamples.
3. Hostile-file and unauthorized-execution tests; no external resource fetching by default.
4. Reproducible evidence export/import, immutable source references and change-triggered invalidation.
5. Jurisdiction source review, applicability tests, effective-date handling and explicit incomplete coverage.
6. Accessibility, cancellation/recovery, clean-machine installation and dependency/license review.
7. Owner review of the exact public files before publication.

## Regulatory scope

Jurisdiction chapters are starter mappings, not exhaustive statements of every law, sector rule or licensed standard. Sources and dates are pinned where available. Direct-text access limitations and future-effective/proposed provisions are identified. Do not redistribute copyrighted standards text without permission. Maintainers must review official updates before publishing or relying on a pack.

## Chapter index

- [00 overview and build order](00_overview_and_build_order.md)
- [01 neutral types and evidence contract](01_neutral_types_and_evidence_contract.md)
- [02 deterministic core and dispatch](02_deterministic_core_and_dispatch.md)
- [03 composition and dependency solver](03_composition_and_dependency_solver.md)
- [04 purpose relative completeness](04_purpose_relative_completeness.md)
- [05 replay and checkpoint experiments](05_replay_and_checkpoint_experiments.md)
- [06 uncertainty and temporal reasoning](06_uncertainty_and_temporal_reasoning.md)
- [07 failure minimization](07_failure_minimization.md)
- [08-secure-evidence-ingestion](08-secure-evidence-ingestion.md)
- [09-mutation-and-metamorphic-testing](09-mutation-and-metamorphic-testing.md)
- [10-independent-checker-and-campaign-reports](10-independent-checker-and-campaign-reports.md)
- [11-generic-risk-pack-engine](11-generic-risk-pack-engine.md)
- [12-packaging-ci-and-reproducible-release](12-packaging-ci-and-reproducible-release.md)
- [13-skrrt-end-to-end-implementation-plan](13-skrrt-end-to-end-implementation-plan.md)
- [chapter14 EU US Risk Packs](chapter14_EU_US_Risk_Packs.md)
- [chapter15 China Australia Risk Packs](chapter15_China_Australia_Risk_Packs.md)
- [16 UI Workbench](16_UI_Workbench.md)
- [17 Document Ingestion Reconciliation](17_Document_Ingestion_Reconciliation.md)

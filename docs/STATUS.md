# Status: what is built, what was run, what is not built

Baseline: design docuseries dated 2026-10-21. This tree implements **increment one** of chapter 13 (a complete narrow
vertical slice). "Tested" below means an automated test in this repository that was actually run; the exact
counts are in the README. Anything not listed as tested is untested.

## Implemented and tested

| Area (docuseries chapter) | Implemented | Evidence |
|---|---|---|
| Contracts (01, 02) | Bundle v1, canonical JSON, domain-separated ids, strict parser (no floats, duplicate keys, BOM, non-finite), handwritten validator with sorted diagnostics | `tests/contract/` incl. frozen identity vector, hash-seed independence |
| Deterministic core (02) | Rule registry, explicit predicate→rule plan, stable ordering, finding validation, rule crash → `ExecutionError`, cooperative cancellation | `tests/unit/test_rules_engine.py`, `test_architecture.py` (no network/process/clock at import) |
| Verdict semantics (00, 04) | Six verdicts; unknown applicability can never become `NOT_APPLICABLE`; missing rule visible as `no_registered_rule`; self-reference excluded | `tests/unit/test_rules_engine.py` |
| Secure admission (08, 17) | Content-addressed store, archive/type-confusion/NUL/size rejection, inert filenames, subprocess sandbox (rlimits, no network, empty cwd, timeout, cancel) | `tests/adversarial/` |
| Extraction (17) | JSON (lexical spans, duplicate-key accounting, exact numerics), Markdown (front matter, key/value tables, inert HTML/code/directives), SQL (lexical splitter, dialect declared or flagged, `CREATE TABLE`/`INSERT` subset, procedural SQL marked unsupported), PDF (native text via optional `pypdf`, image-only pages, encrypted, corrupt, limits) | `tests/unit/test_adapters.py`, `tests/adversarial/` |
| Coverage ledger (17) | Per-region coverage, derived file summary, byte-accounting property test for text formats | `test_adapters.py` |
| Review & reconciliation (16, 17) | Append-only decisions, `If-Match`, idempotency keys, reason rules, proposals only | `tests/e2e/test_workflow.py` |
| Jobs & staleness (16) | State machine, sequenced events, cancel (queued/running/after success), restart recovery, stale detection from claim/evidence/extraction/profile changes | `tests/e2e/test_workflow.py` |
| Package + independent checker (10, 12) | Deterministic export, hash manifest, clean-room checker that re-derives verdicts; negative controls (always-`SUPPORTED` producer, flipped verdict with recomputed ids, forged witness, dropped finding, partial run, traversal zip) | `tests/e2e/test_workflow.py::Tamper`, `AlwaysPassBaseline` |
| Local server (16) | Loopback only, Host/Origin/token checks, CSP, body limits, keep-alive framing | `tests/e2e/test_server.py` |
| Workbench UI (16) | Five-step flow, coverage tables, two-pane review with highlighted evidence, bulk-approve preview, run builder, job tray with cancel, findings with evidence snippets, export + checker | `tests/e2e/browser_journey.cjs` (run manually; one Chromium) |
| CLI (00, 12) | `import candidates approve reject check export verify-package validate serve`, exit-code contract, no overwrite | `tests/e2e/test_cli.py` |

## Added in increment two (chapters 03 and 06, partial)

| Area | Implemented | Evidence |
|---|---|---|
| Dependency graph (03) | `graph.py`: stable Kahn ordering, real cycle witnesses (not all residual nodes, bounded and flagged when truncated), missing-input closure kept separate from ordering, producer collisions, conflicting declarations, exact invalidation paths. A `graph` summary is written into every `result.json`. | `tests/unit/test_graph_temporal.py` |
| Temporal reasoning (06) | `temporal.py`: exact-Decimal closed intervals, strict precedence, half-open positive overlap under uncertainty, logical clocks that never compare across clock ids, possibility sets, happens-before reachability. Overlap/missing data is `UNKNOWN`. | same |
| `interval.relation` rule | Document-checkable claims (`precedes`, `no_overlap`, `overlaps`) over facts `<name>.start|end` with optional `_min/_max` uncertainty. Two durations never prove overlap; conflicting sources go to human review. Editable in the review form. | unit + `IntervalWorkflow` e2e |

The independent checker does **not** yet cover `rule.interval_relation`; packages containing it verify but list it under
`unsupported`. Composition *obligations* (tags, generations, unit conversion edges) and the cache-key scheme are not built.

## Deliberate deviations from the docuseries

* **UI stack:** vanilla JavaScript served as static files (no React/TypeScript, no build step) to keep the slice
  dependency-free and auditable. API types are not generated; the contract is the JSON in `server.py`/`service.py`.
* **Events:** job progress is polled (`GET /jobs/<id>/events?after=<seq>`), not server-sent. The sequence/resume
  contract is implemented; the push transport is not.
* **Process isolation:** POSIX `resource` limits plus a socket guard inside the worker. This is containment against
  accidents and malformed input, **not** a hardened sandbox (no seccomp/namespaces). On non-POSIX platforms only the
  wall-clock timeout applies.
* **Race-resistant file opening** (chapter 08) is unnecessary here because uploads arrive as bytes over HTTP/CLI
  argument reads, not as directory trees; directory-package admission is not implemented.

## Not built yet (explicitly out of this increment)

Replay/checkpoint experiments, purpose profiles, composition obligations,
failure minimization, mutation/metamorphic campaigns and campaign reports (chapters 03–07, 09, most of 10);
the generic risk-pack engine and the EU/US/China/Australia packs (11, 14, 15) — **no jurisdiction content exists
here**; OCR; signatures and trust anchors; model-assisted suggestions; manual creation of claims from free prose;
manual claim editing beyond the three predicates; dependency-graph and temporal-replay views; multi-project support.

## Release gates (README of the docuseries) — honest state

1. UI + CLI acceptance on all four formats — **partial**: all four formats run in tests; the browser journey covers
   Markdown/JSON/SQL; PDF is exercised through the service-level tests only (not through the browser, HTTP or CLI tests).
2. Controls, checker disagreement, replay, reduction — **partial**: positive/negative controls and the checker exist;
   replay and reduction do not.
3. Hostile files / no execution / no fetching — **partial**: covered for the implemented adapters; no fuzzing campaign yet.
4. Reproducible export, immutable source refs, change-triggered invalidation — **done for this slice** (cross-workspace
   byte-identical packages except observation metadata; staleness tests).
5. Jurisdiction review — **not started**.
6. Accessibility, recovery, clean-machine install, license review — **partial**: keyboard path, skip link, focus handling,
   `aria-live` status and non-colour verdict labels are in place and smoke-tested; **no screen-reader or automated audit
   was run**; a wheel was installed and exercised in one fresh virtualenv on one platform; **no dependency/license review**.
7. Owner review before publication — **pending**.

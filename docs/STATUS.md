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

The independent checker re-derives `rule.interval_relation` with its own corner-case formulation; a 400-case differential
test against the producer found and fixed an incomplete "impossible overlap" case in `temporal.positive_overlap`.
Composition *obligations* (tags, generations, unit conversion edges) and the cache-key scheme are not built.

## Added in increment three (chapters 05, 07, 11)

| Area | Implemented | Evidence |
|---|---|---|
| Replay and checkpoint (05) | `subjects/reference_store.py` (hand-written field inventory: entries, next id, tick, random cursor, operation cursor; atomic one-unit transactions), a separately written mutable candidate with five injectable defects, `replay.py` (identity-checked checkpoints, pure replay plans that refuse gaps/duplicates/wrong generation/version instead of repairing), three-way experiment comparing the decoded state at **every** post-boundary step with field-level witnesses | `tests/unit/test_experiments.py` |
| Crash model (05) | Virtual durable store, exhaustive enumeration of every crash boundary, declared coverage (`exhaustive` or `truncated_by_budget` with the unexplored positions listed), writer and recovery defects (`marker_before_flush`, `join_members`, `silent_fallback`) | same |
| Failure minimization (07) | `minimize.py` + `reducers/`: failure *signature* (positions excluded), five-way oracle outcome, ddmin chunk deletion over whole units, a dedicated transaction-member reducer, value/key/seed simplifiers with strictly decreasing lexicographic cost, graph-cycle reducers (edge deletion, node deletion with dependent closure, unreferenced-node deletion), oracle cache that never caches operational errors, flaky originals reported `UNSTABLE`, honest scope statements | `tests/unit/test_minimize.py` |
| Counterexample package (07) | Original and reduced case, signature, witness, full reduction log, verified by **reproduction in a fresh interpreter** that shares no state with the reducer; tampered cases/signatures/witnesses are rejected even with a recomputed package id; a deliberately lax oracle is caught | same |
| Risk-pack engine (11) | `riskpacks.py`: data-only packs (unknown or executable-looking fields rejected), bounded predicate grammar (eq/in/exists/and/or/not, no regex, no coercion), three-valued applicability with full truth-table tests, content-digest pinning, conflicting content under one identity rejected, `requires` with cycle detection, machine-readable pack diff, obligation planning and conjunctive aggregation that keeps member findings, remediation *proposals*, one synthetic example pack (a fictional example, not a policy) | `tests/unit/test_riskpacks.py`, `tests/e2e/test_riskpack_workflow.py` |
| Packs in runs, UI, checker | Pack bytes and context facts travel inside the bundle, pinned by digest in `profile_refs`; `result.json` gains an obligations layer; the checker re-derives applicability with its own three-valued evaluator and re-aggregates verdicts, and rejects flipped, dropped or laundered-`NOT_APPLICABLE` obligations, edited packs and altered context | `PackTamper` tests |
| UI | Optional "Risk packs" panel on Run (selection and context facts, never assumed false), obligations table on Findings, an Advanced page for experiments/minimization/crash enumeration. Experiment endpoints are bounded (≤ 400 operations, ≤ 3000 oracle calls) and operate only on the fictional store. | browser journey, `PacksAndExperiments` |
| CLI | `experiment`, `minimize`, `verify-counterexample`, `pack list/validate/install/diff`, `check --pack --context` | manual runs; `test_cli` covers the older commands only |

Limits worth knowing: the checkpoint experiment and the minimizer share the experiment runner (reproduction in a fresh process is
independent of the *reducer*, not an independent reimplementation of the store semantics); the crash model is a simulation;
the experiment tools are a laboratory demo on a fictional store, not a way to test an arbitrary external program yet (there is no
adapter for running a user-supplied subject). **No jurisdiction pack exists**: chapters 14-15 are untouched.

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

Purpose profiles (04), composition obligations (03), mutation/metamorphic campaigns and campaign reports (09, most of 10),
subject adapters for user-supplied programs; the EU/US/China/Australia packs (14, 15) — **no jurisdiction content exists
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

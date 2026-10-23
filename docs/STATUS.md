# Status: what is built, what was run, what is not built

Baseline: design docuseries dated 2026-10-21. The narrow vertical slice and eight subsequent increments
implement much of the generic laboratory; the full docuseries and release review are not complete.
See [BUILD_PROGRESS.md](BUILD_PROGRESS.md) for the chapter-by-chapter assessment and remaining work.
"Tested" below means an automated check actually run, not a design acceptance claim.

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

## Added in increment four (chapters 09 and 10)

| Area | Implemented | Evidence |
|---|---|---|
| Mutation operators (09) | Ten operators over hand-written synthetic scenarios, each with preconditions, a committed expected outcome and a soundness note; semantic and byte layers labelled separately; mutations are non-destructive and recorded (base/result digest, generator version, seed) | `tests/unit/test_campaign.py` |
| Metamorphic relations (09) | Six relations (key order, opaque renaming, irrelevant artifact, removing the only witness, increasing uncertainty, processing order), each with a satisfying and a precondition-failing example. "More evidence never hurts" is deliberately not a relation. | same |
| Campaigns (09, 10) | Deterministic digest-pinned manifest, `PASSED / FAILED / SKIPPED_PRECONDITION / ERROR` accounting (skips never count as passes), operator/rule/relation coverage, surviving mutations listed, report digest | same |
| Negative controls | Four defective kernels (always-SUPPORTED, always-CONTRADICTED, last-key-wins, self-reference allowed); every one fails the campaign | same |
| Independent report checker (10) | `campaign_checker.py` (no producer imports): exact case set, unique ids, recomputed statuses and totals, hidden survivors, skip laundering, altered expectations, report digest; a self-consistent weakened manifest is accepted **only** if no trusted digest is supplied, and is then labelled as the narrower conclusion | same; `Campaigns` server test |
| UI / CLI | Campaign page (kernel select, headline, denominators, coverage, survivors, every case, forged-report demo), `verifier-lab campaign run\|verify` | browser journey |

The first campaign found a real defect in the production rules (`interval.relation` reported `INSUFFICIENT_EVIDENCE` for corrupt
evidence instead of `INVALID_EVIDENCE`); it is fixed and has a regression test. Scope: two small synthetic bases, ~70 cases. The
detection ratio is reported with its denominator and is not a safety score. Not built: user-authored relations, automated
shrinking of campaign failures through the minimizer, bundle-level (rather than scenario-level) mutation of exported packages.

## Added in increment five (chapter 05, subject adapters)

| Area | Implemented | Evidence |
|---|---|---|
| Subject adapter | `subjects/external.py`: JSON-lines protocol (`init/apply/snapshot/load` + optional `replay`) to a user-supplied program; a fresh process per instance; empty working directory, minimal environment, rlimits, per-request timeout, response size cap; crashes/hangs/protocol violations become execution errors, never divergences or passes; identity = command + file bytes + environment | `tests/unit/test_external_subject.py` |
| CLI only | `--subject-cmd/--subject-env` on `experiment`, `minimize`, `verify-counterexample`; the HTTP API refuses any command field; a counterexample package records only the subject's identity and **never** causes a command to run - verification needs the caller to supply a program with the same identity | same; `NoCommandsOverHttp` |
| Example | `examples/reference_subject.py`, a standalone store with optional injected defects (`SUBJECT_DEFECTS`) to start from | same |

Containment is accidental-damage protection, not a sandbox for hostile code. The campaign page still judges only the built-in
verifier kernels; pointing a campaign at an external verifier program is not built.

## Added in increment six (chapter 04, purpose-relative completeness)

| Area | Implemented | Evidence |
|---|---|---|
| Purpose profiles | `purposes.py`: data-only, digest-pinned, immutable versions; roles with explicit cardinality, value kind and optionality; an invalid profile (min > max, cyclic lifecycle, unknown fields) is a *profile* error, not a subject failure; mechanical `strengthens()` relation that never compares names | `tests/unit/test_purposes.py` |
| Assessment | Roles are bound by naming fact keys (never inferred from file names). Per-obligation verdicts for roles, a `same_value` coherence rule (torn generations fail despite valid hashes) and a lifecycle graph (prerequisites must be recorded events, not boolean flags; a later same-clock invalidation beats an older success; different clocks are ambiguous and keep both records). No overall SUPPORTED while any obligation is unmet or unexecuted; each assessment lists what could resolve it and what was outside scope | same + fixture matrix |
| In runs and exports | Profile and bindings are bundle artifacts pinned by digest; `result.json.purposes`; purpose-only runs (no claims) allowed; a newer installed version is *offered*, never applied to an old report; the independent checker re-derives every obligation from verified token bytes and rejects laundered, dropped, hidden or invented assessments | `tests/e2e/test_purpose_workflow.py` |
| UI | Purpose panel on Run (role-by-role binding), Purpose assessments on Findings with per-obligation reasons, witnesses and "what could resolve it" | browser journey |

Scope: two example profiles about a fictional checkpoint (`inspect-checkpoint`, `resume-exactly`); coherence supports only `same_value`;
lifecycle history is read from `<history>[i].stage|seq|clock` facts. Not built: richer coherence rules, user-authored profile editing in the UI,
and applicability conditions on profiles (a profile that is `NOT_APPLICABLE` to a claim).

## Added in increment seven (chapter 03, composition obligations)

| Area | Implemented | Evidence |
|---|---|---|
| Composition analysis | `composition.py`: pipeline descriptions (data only, strict shape, bounded) with a deliberately tiny vocabulary - exact tag equality, finite membership, generation equality, explicit prerequisites, declared unit conversions verified only against an exact registered table (no conversion is inferred from familiar unit names). Ordering (stable Kahn, real cycle witness) is kept separate from closure (undeclared inputs, external inputs without an assumption). Every obligation keeps its own verdict; support needs all of them; assumptions (declared externals, unverified producer tags) stay visible; targeted claims evaluate only a target's ancestors and say the rest is unresolved, not verified | `tests/unit/test_composition.py`, `fixtures/composition/` |
| Invalidation | `impact(old, new)`: changed/added/removed activities and external inputs, each affected activity with a witness path; unaffected branches listed | same |
| Rule and checker | `composition.valid` rule; the independent checker re-derives every obligation with its own implementation (plain dict/set code, a different cycle test); a 500-pipeline randomized differential test found the two agree; laundered or hidden obligations are rejected | same; `tests/e2e/test_composition_workflow.py` |
| UI | Composition page (source, scope, SVG layered diagram *plus* an equivalent edge table, obligations, version comparison), composition option in the review form, obligations in the finding detail | browser journey |

Limits: the registered conversion table has five pairs; requirements are equality/membership only (no ranges or arithmetic); a pipeline is read
from one evidence document (conflicting copies go to human review); producer tags are assertions, so a SUPPORTED result says so in `conditional_on`.
The cache-key scheme for incremental re-evaluation (claim + transitive evidence + rule versions) is not implemented; impact analysis is.

## Added in increment eight (adapters for campaigns, format documentation)

* `--kernel-cmd`: a campaign can judge a verifier *program* you supply (`harness/external_kernel.py`; fresh process per scenario; crashes, hangs and malformed answers are `ERROR` cases,
  never passes; the report names the kernel by the identity of command, file bytes and environment). CLI-only; the HTTP API refuses `kernel_cmd`. Example: `examples/reference_verifier.py`
  (fact predicates only, so the campaign honestly shows what it does not cover). Tests: `tests/unit/test_external_kernel.py`.
* `docs/FORMATS.md`: written specification of risk packs, purpose profiles, pipeline descriptions, campaign manifests/reports, counterexample packages and both program protocols
  (the Open Canopy contract asks that important formats be documented).

## Added in increment nine (chapter 12, release hardening)

* `tools/check_release.py`: two isolated wheel builds with pinned, hash-verified Python build inputs,
  deterministic timestamps and matching artifact hashes; package-content allowlist and negative tests;
  fresh offline wheel installation outside the checkout, four-format CLI acceptance and independent export checking.
* Installed UI startup/assets and optional Chromium journey. PDF now travels through the browser's
  import, partial-coverage display, review, check, contradiction and independently verified export path.
* `.github/workflows/ci.yml`: full Python suite and installed-wheel browser/release gates on Linux/Python 3.12.
  The workflow has been added; a remote CI run is not implied by local validation.
* [RELEASE.md](RELEASE.md): commands, dependency inventory and evidence limits. The helper records source-input,
  dependency and artifact hashes and dirty source state; it does not publish or sign anything.

## Added in increment ten (source-linked human interpretation)

* A user can create a pending candidate from selected prose in an immutable source rendition,
  give an interpretation reason, review it, and run it against separate evidence. The UI has an
  equality form; service and CLI accept any installed built-in predicate with validated arguments.
* Selected spans and authoring reasons are separate from extraction records, which stay unchanged.
  Human candidates remain qualified and cannot receive unqualified bulk approval. Export and the
  independent checker retain the distinction between source-byte integrity and human interpretation.
* `source` and `author` CLI commands; `POST /api/v1/candidates`; Unicode-safe UI selection. Tests cover
  persistence, stale sources, input validation, authorization and provenance forgery.
* An asynchronous import refresh rechecks file selection before replacing the form, preserving files
  selected while the refresh's requests were in flight.

## Added in increment eleven (the other synthetic systems)

* `subjects/pipeline.py` and `subjects/scheduler.py`: separately implemented reference/candidate
  transitions for versioned document transformations and a seeded logical-time scheduler. Four
  intentional defects cover stale cache reuse, dropped revisions, deadline ties and random continuity.
* `experiments/laboratory.py`: full-state three-way comparison, bounded operation deletion with
  stable failure signatures, version-pinned counterexample packages, original/reduced witness checks
  and fresh-process reproduction. Shared serialization is not shared transition semantics.
* CLI `experiment pipeline|scheduler` and `minimize --system ...`; bounded HTTP endpoints; system
  selection in the Advanced page; downloadable counterexamples. Existing KV workflows remain available.
* 361 Python tests passed, including randomized reference agreement, invalid inputs, all checkpoint
  boundaries, all injected defects and rehashed forgeries. Installed-wheel CLI and Chromium checks passed.

## Added in increment twelve (OS parser boundary)

* Default parsing requires Linux user/mount/PID/network namespaces through Bubblewrap and a native
  syscall filter through libseccomp. Workspace files and credentials are absent from the worker's
  mounts. Network syscalls, fork/exec and namespace/privilege operations are denied after bootstrap.
* Missing isolation fails closed. Explicit `--parser-isolation portable` is trusted developer
  configuration, recorded in extraction limits/capabilities, and is not a hostile-code boundary.
* Parent stdout/stderr capture is bounded during execution. Native syscall and host-filesystem
  probes supplement malformed-document tests; cancellation and memory-limit errors stay operational.
* CI installs the required OS packages. See SECURITY.md and RELEASE.md for validated scope.

## Added in increment thirteen (automated accessibility gates)

* The installed-wheel browser journey runs axe-core WCAG 2 A/AA and WCAG 2.1 A/AA checks on
  import, reviewed candidate, run form, findings, advanced results, campaign, composition, export
  and source-authoring screens. An intentionally unlabeled button proves the gate can fail.
* Accessible names on the project summary and source/reproducer text now have explicit region roles.
* The release checker requires every requested audit to execute, records engine/version and manual
  review targets, and retains `accessibility.json`. CI installs the pinned test-only audit tool.
  Incomplete/manual checks remain visible; a screen-reader audit is still outstanding.

## Added in increment fourteen (extraction retry recovery)

* Explicit re-extraction always creates a new immutable extraction attempt, even when an earlier
  attempt with the same source bytes and dialect succeeded. Ordinary same-byte imports remain
  deduplicated.
* Source views distinguish the currently usable extraction from the latest attempt. Thus a failed
  retry is visible as an operational error without invalidating candidates that remain bound to the
  earlier successful attempt.
* `verifier-lab reextract SOURCE_ID [--dialect ...]` and workbench retry controls expose the same
  shared-service behavior for all admitted formats; SQL defaults to its most recently declared
  dialect.
* Focused retry tests also passed in the required Linux/Python 3.12 CI target with Bubblewrap
  enabled. The recovered Windows workspace remains explicit portable-development evidence only;
  it is not a local security-validation claim.

## Added in increment fifteen (HTTP failure states)

* Non-numeric, negative or blank `limit`/`offset`/`after` query values and mistyped run-request lists/context
  previously produced HTTP 500; they are now 422 client errors (`bad_query`, `bad_run_request`).
* `FailureStates` tests cover unknown ids, bad framing, wrong JSON shapes and undecodable names. Seven new tests
  passed on the recovered Windows workspace; the rest of the suite there still fails closed in required parser mode
  (same 104 failures/18 errors with and without this change). The Linux/Bubblewrap suite has not been run for this increment.
* CLI: a missing, unreadable or non-JSON input file (`validate`, `pack`, `campaign verify`, `--context-file`) now
  gives a one-line error and exit code 4 instead of a traceback (`CliInputFailures`).
* Experiment endpoints validate defect lists, boundaries, crash `max_positions` and minimizer budgets as 422 client errors
  (previously a mistyped crash/defect parameter returned 500).
* The run endpoint accepts the workbench's purpose request objects and rejects scalar entries. A regression
  in the initial failure-state validator caused the purpose browser journey to stall; the hosted release gate
  exposed it, and the corrected request shape has a focused HTTP test.
* Explicit false or null run lists, purposes and context are now rejected as malformed requests instead of
  being interpreted as omitted values. Decision actions and reasons have type checks, so non-string values
  produce 422 client errors before decision processing.

## CI host capability note

GitHub's default hosted-job sandbox blocked Bubblewrap from creating its required nested user
namespace, causing parser imports to fail closed with `isolation_unavailable`. The Linux CI job
therefore runs in an explicitly privileged Ubuntu 24.04 container; this supplies the *test host*
capability for Bubblewrap/libseccomp rather than weakening the application boundary or selecting
portable parser mode. The minimal container also needs `libsqlite3-0` because the hosted CPython
`_sqlite3` extension dynamically links against the image's system SQLite runtime. CI uses the
container's Ubuntu Python 3.12 in a fresh virtual environment rather than the hosted toolcache,
so the interpreter's dynamic libraries remain available in Bubblewrap; it verifies the SQLite
import before it starts the suite. On 2026-10-23, the privileged hosted run passed the complete
suite plus clean installed-wheel, Chromium journey and automated accessibility gates. Its exact
remote outcome is CI evidence, not a cross-platform claim.

## Deliberate deviations from the docuseries

* **UI stack:** vanilla JavaScript served as static files (no React/TypeScript, no build step) to keep the slice
  dependency-free and auditable. API types are not generated; the contract is the JSON in `server.py`/`service.py`.
* **Events:** job progress is polled (`GET /jobs/<id>/events?after=<seq>`), not server-sent. The sequence/resume
  contract is implemented; the push transport is not.
* **Process isolation:** required Linux parser mode uses Bubblewrap namespaces, read-only code/runtime mounts and
  libseccomp native syscall denial, plus resource/time/output limits. Explicit portable development mode has weaker
  guarantees; explicitly supplied external programs still have their separate documented resource containment.
* **Race-resistant file opening** (chapter 08) is unnecessary here because uploads arrive as bytes over HTTP/CLI
  argument reads, not as directory trees; directory-package admission is not implemented.

## Not built yet (explicitly out of this increment)

The EU/US/China/Australia packs (14, 15) — **no jurisdiction pack ships here**; OCR; signatures and trust anchors;
model-assisted suggestions; broader claim predicate editing;
temporal-replay and general dependency-graph views; multi-project support.
Campaigns, replay and reduction are implemented as described above, with their stated scope limits.

## Release gates (README of the docuseries) — honest state

1. UI + CLI acceptance on all four formats — **implemented for the narrow tutorial scope**: the release helper
   exercises all four from an installed wheel; the browser journey includes PDF review and export.
2. Controls, checker disagreement, replay, reduction — **implemented within the documented scope**: positive/negative
   controls, independent checker tamper tests, three-way KV replay and fresh-process reduced reproducer verification.
   This does not establish the broader multi-system laboratory milestone.
3. Hostile files / no execution / no fetching — **partial**: covered for the implemented adapters; no fuzzing campaign yet.
4. Reproducible export, immutable source refs, change-triggered invalidation — **done for this slice** (cross-workspace
   byte-identical packages except observation metadata; staleness tests).
5. Jurisdiction review — **not started**.
6. Accessibility, recovery, clean-machine install, license review — **partial**: keyboard path, skip link, focus handling,
   `aria-live` status and non-colour verdict labels are in place and smoke-tested; **no screen-reader audit was run**; automated WCAG A/AA checks cover nine populated screens; repeatable installed-wheel acceptance and build comparison are available for Linux/Python 3.12;
   a dependency metadata inventory exists, but **full dependency/provenance/license review remains open**.
7. Owner review before publication — **pending**.

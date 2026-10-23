# Formats and protocols

Everything this tool reads or writes is plain JSON (or the original source files) with a written specification, so that a person or another
program can understand, migrate or re-implement it without this code. The export package itself is specified in
[`PACKAGE_FORMAT.md`](PACKAGE_FORMAT.md). This page covers the rest. All JSON uses the strict profile: UTF-8, no duplicate keys, no
floats or NaN in normative records, object keys sorted when hashed, compact separators. Identities are
`<kind>:sha256:<hex>` over `kind + 0x00 + canonical body` (without the record's own id); file digests are `sha256:<hex>` of raw bytes.

## Risk pack (`verifier-lab/riskpack/1`)

Fields: `schema, pack_id, version, title, description?, compatible_bundle_versions[], applicability, obligations[], source_refs[], requires[]?, content_digest`.
Unknown fields are rejected (packs are data, never code). `content_digest` = SHA-256 of the canonical body without that field.
An **obligation**: `obligation_id, claim_predicate, rule_ref{rule_id, version}, subject?, applicability?, evidence_requirements[{id, description?}], review_requirements[],
classification_requires_human?, title?`. `rule_ref` must name an installed rule that implements `claim_predicate`.
**Predicate grammar** (applicability): `{"op":"eq","fact":K,"value":V}`, `{"op":"in","fact":K,"values":[..]}`, `{"op":"exists","fact":K}`,
`{"op":"and"|"or","args":[..]}`, `{"op":"not","arg":P}`; depth <= 8. Evaluation is three-valued: a fact that is not stated is `UNKNOWN`, `null` means
explicitly absent, types are never coerced. Obligation verdict over matching claims: any `CONTRADICTED` -> `CONTRADICTED`; all `SUPPORTED` -> `SUPPORTED`; else
`HUMAN_REVIEW_REQUIRED`, then `INVALID_EVIDENCE`, else `INSUFFICIENT_EVIDENCE`.

## Purpose profile (`verifier-lab/purpose/1`)

Fields: `schema, profile_id, version, title, description?, roles[], coherence[]?, lifecycle?, content_digest`.
**Role**: `role, description?, min (default 1), max (default 1, null = unbounded), value_kind (any|number|integer|decimal|string|boolean), optional?`
(`optional` requires `min` 0; `min > max` is an invalid profile). **Coherence**: `{id, type:"same_value", fact}` - every source that supplies a role must state
`fact` with the same value. **Lifecycle**: `{target, history, stages[], requires{stage:[prerequisites]}, invalidating[]}`; the graph must be acyclic.
History is read from facts `<history>[i].stage` (string), `.seq` (integer), `.clock` (string): events on one clock order by `seq`, different clocks are
unordered. Role **bindings** (supplied per run) map a role to one or more fact keys; roles are never inferred from file names.
Per-obligation ids: `role:<role>`, `coherence:<id>`, `lifecycle:records|prerequisites|current_validity`.


### Purpose applicability (`applies_when`, optional)

A profile may carry `applies_when`: a predicate in the risk-pack grammar (`eq|in|exists|and|or|not`, three-valued, no coercion) over the
run's **context facts** (the same `--context KEY=VALUE` facts risk packs use). It states when the purpose is the right question to ask, not what
the evidence says. Evaluation adds a first obligation, `applicability`, and never lets a missing fact become "not applicable":

| `applies_when` | `applicability` obligation | other obligations | overall |
|---|---|---|---|
| TRUE (or field absent: no obligation is added) | `SUPPORTED` / `applicability_established_true` | evaluated as usual | as usual |
| FALSE | `NOT_APPLICABLE` / `applicability_established_false` | **not evaluated** and listed as not evaluated | `NOT_APPLICABLE` |
| UNKNOWN | `INSUFFICIENT_EVIDENCE` / `applicability_unknown`, naming the facts that would resolve it | evaluated so the reader still sees them | never `SUPPORTED`; `CONTRADICTED`/`INVALID_EVIDENCE` still win |

The obligation's witness records the facts consulted and their values. Context facts travel in the bundle (`context-facts+json`) whenever a risk pack or an
applicability-bearing purpose is part of the run, so the independent checker re-derives the result with its own evaluator and rejects a laundered
`NOT_APPLICABLE`, a dropped obligation or altered context. `strengthens()` refuses to compare profiles when either has `applies_when` (reason
`applicability_conditions_not_comparable`), because "needs less evidence" is meaningless across different scopes. The decision is data-only: a profile can never fetch,
execute or configure anything. Profiles without `applies_when` behave exactly as before. A human cannot override FALSE here; a reviewer who believes the facts are wrong
changes the context and reruns, which keeps the decision auditable.

## Pipeline description (read by `composition.valid` and the Composition page)

A JSON object under a key you choose (default `pipeline`): `{external[], activities[], same_generation[]}`.
`external[]`: `{artifact, tags{}, generation?, assumption?}` (an external input without an `assumption` is unresolved).
`activities[]`: `{id, consumes[{artifact, requires{tag: value | {equals} | {in:[..]}}}], produces[{artifact, tags{}, generation?}], depends_on[ids], conversion?{from,to,factor}}`.
`same_generation`: lists of artifact names that must share a generation. A `conversion` is *verified* only if `(from,to,factor)` equals an entry of the registered exact
table (`seconds->milliseconds 1000`, `milliseconds->seconds 0.001`, `minutes->seconds 60`, `milliseconds->microseconds 1000`, `microseconds->milliseconds 0.001`).
Obligation ids: `graph:declarations|self_dependency|references|acyclic`, `conversion:<activity>`, `input:<activity>:<artifact>`, `generation:<n>`.

## Campaign manifest and report (`verifier-lab/campaign-manifest/1`, `.../campaign-report/1`)

Manifest: `{schema, generator{version, algorithm, seed}, bases{id: digest}, cases[], manifest_digest}`; a case: `id, layer (semantic|byte|metamorphic), kind, base, claim?, predicate?,
operator?|relation?, seed, precondition_holds, expected, ..., digest`. Report: `{schema, manifest_digest, kernel{name}, base_expectations_reproduced{}, cases[], summary, survivors[], report_digest}`.
Case status vocabulary (never domain verdicts): `PASSED, FAILED, SKIPPED_PRECONDITION, ERROR`. `summary` is recomputable from `cases`; a verifier must reject a report whose totals,
survivors or statuses cannot be recomputed. A manifest digest must be obtained from a trusted channel to show a report belongs to the *intended* campaign.

## Counterexample package (`verifier-lab/counterexample/1`)

See `PACKAGE_FORMAT.md`. A package that names an external subject records its *identity* only (hash of command, file bytes and environment); it never carries a runnable command.

## Subject program protocol (JSON lines over stdin/stdout)

`{"cmd":"init","seed":S}`, `{"cmd":"apply","op":{..}}` -> `{"ok":true,"accepted":bool,"observations":[str]}`, `{"cmd":"snapshot"}` -> `{"ok":true,"state":{entries,next_identifier,logical_tick,random_cursor,operation_cursor}}`,
`{"cmd":"load","state":{..}}`, optional `{"cmd":"replay","ops":[..]}` (answer `{"ok":false,"error":"unsupported"}` to fall back to sequential `apply`).
Operations: `{"op":"put","key","value"}`, `delete`, `allocate`, `draw` (each with `key`), `{"op":"txn","ops":[..]}`. See `examples/reference_subject.py`.

## Verifier program protocol (campaigns)

Request `{"cmd":"verify","scenario":{id, origin{name,fmt,text,sha256}, evidence[{..}], claims[{key,predicate,subject,arguments}]}}`; response
`{"ok":true,"outcomes":{"<claim key>":["<VERDICT>","<reason_code>"]}}`. One fresh process per scenario. See `examples/reference_verifier.py`.

Both program protocols are CLI-only (`--subject-cmd`, `--kernel-cmd`): the web API refuses commands, and nothing imported can select a program to run.

## Human-authored source-linked candidates

`POST /api/v1/candidates` creates a **pending** human interpretation, not an extracted fact or an
approved claim. Supply `source_id`, the displayed current `attempt_id`, `byte_start` and exclusive
`byte_end` in the UTF-8 text rendition, an `interpretation` object (`predicate`, `subject`,
`arguments`), and a nonempty `reason`. A selection must cover nonblank text, lie on UTF-8 boundaries
and contain at most 16000 bytes. A changed or replaced source requires reloading and selecting again.
An optional `Idempotency-Key` deduplicates retries and rejects reuse with different input.

The UI offers selection from read-only source text and equality claim entry. The CLI exposes
`source <source-id>` and `author <source-id> --attempt <attempt-id> --start <byte> --end <byte>
--subject <fact-key> --arguments '<JSON object>' --reason '<interpretation reason>'`, with
`--predicate` for other installed built-in rules. A separate normal review decision remains required.
Authored candidates are excluded from unqualified bulk approval.

Original extraction records and their coverage are unchanged. Selected spans are stored separately
and compiled into provenance with grammar `reviewed-text` (the exact UTF-8 token as a string), an
explicit human authoring reason, and `semantic_entailment: not_machine_verified`. The independent
checker verifies that byte linkage and labeling; it does not infer that free prose logically entails
the reviewed claim. Authored interpretations never become evidence facts. Source replacement and
claim revision changes retain history and mark dependent runs stale through the existing rules.

## Pipeline and scheduler experiment cases

The laboratory also supplies two fictional subjects, selected by the literal names `pipeline` and
`scheduler`; neither a case nor a counterexample can select code or a command. Each has independently
written reference and candidate transitions. `verifier-lab experiment pipeline|scheduler --ops 12`
compares every state field across continuous, checkpoint/resumed and reference-checkpoint replay paths.
The Advanced page exposes the same operations and intentional defect controls.

Pipeline operations are `put` (`key`, `value`) and `transform` (`key`, `mode`: `upper|reverse`, `output`).
The explicit state inventory is documents with source revisions, outputs with source revisions,
revision-keyed transform cache, and operation cursor. `stale_cache` incorrectly reuses an older source;
`drop_versions` omits revision continuity from serialized state. Scheduler operations are `schedule`
(`key`, `delay`), `advance` (`ticks`), and `cancel` (`key`). Logical delays/ticks are integers in 0–1000.
Its inventory is logical time, next allocation sequence, random cursor, pending jobs, completed
observations and operation cursor. Equal deadlines run in allocation order. `reverse_ties` violates
that order; `drop_random_cursor` loses random continuity in a checkpoint. Rejected operations leave
all state fields unchanged. These are bounded demonstrations, not production schedulers or parsers.

Cases record `system`, `seed`, `ops`, checkpoint `boundary` and `defects`. At most 400 operations and
a 100-character seed are admitted. Pipeline values are bounded to 1000 characters and names to 64.
Use `verifier-lab minimize --system pipeline|scheduler --defect <name> --ops 12 --output case.json`,
then `verifier-lab verify-counterexample case.json` to reproduce in a fresh interpreter. Existing KV
store commands remain unchanged. The UI can download the same counterexample package.

Schema `verifier-lab/laboratory-counterexample/1` pins runner/subject semantics versions, original
and reduced case identities, a position-independent failure signature, exact witness and reduction
log/scope. Verification re-evaluates both original and reduced cases, checks subject/seed/defect
continuity, signature equality, cost and the exact witness. The reducer only deletes whole operation
chunks/single operations while adjusting the boundary. Its exhausted scope never implies global
minimality. A 3–3000-call budget includes the mandatory reproducibility checks. Fresh-process
reproduction is independent of reducer memory; it shares the runner and is not an independent-language
implementation. Unknown semantics versions, forged witnesses and changed failure signatures are rejected.

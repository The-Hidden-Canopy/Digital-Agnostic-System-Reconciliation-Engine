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

# 02 Deterministic core and dispatch

## Purpose and module boundary

Implement canonical encoding, deterministic identity, rule registration, and dispatch without execution side effects. Modules are `canonical.py`, `registry.py`, and `engine.py`. The core accepts validated immutable objects and returns immutable findings. It never reads wall-clock time, selects a random seed, discovers plugins, or opens arbitrary artifact paths on a rule's behalf.

Determinism is scoped. Identical validated inputs, rule versions, declared configuration, and implementation version must produce identical normative output bytes. This does not promise identical behavior for unpinned third-party plugins or all future Python versions. Maintain conformance vectors across supported runtimes and record the runtime identity in non-normative execution metadata.

## Canonical encoding algorithm

First validate the restricted JSON value model from chapter 01. Sort object keys using Python string code-point ordering, encode strings with JSON escaping and strict UTF-8, emit integers in ordinary decimal notation, and use compact separators. Preserve list order unless the schema declares the list set-like. Normalize set-like record collections by ID before encoding the bundle. Never sort a list merely because its current elements happen to appear sortable.

```python
def canonical_bytes(value: JsonValue) -> bytes:
    validate_canonical_value(value)
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False,
    ).encode('utf-8', errors='strict')

def record_id(kind: str, body_without_id: JsonValue) -> str:
    payload = kind.encode('ascii') + b'\x00' + canonical_bytes(body_without_id)
    return kind + ':sha256:' + hashlib.sha256(payload).hexdigest()
```

The domain separator prevents identical JSON bodies of different record types from sharing an identity. Parse IDs and validate their prefix instead of treating any hash-looking string as an equivalent identifier. Artifact digests hash bytes directly and therefore use a distinct API from record IDs.

Use a duplicate-key rejecting `object_pairs_hook` with `json.loads`. Use `parse_constant` to reject nonstandard constants and a `parse_float` callback that raises a validation diagnostic. Input byte limits precede parsing; depth limits require either a bounded pre-scan or a parser strategy that avoids accepting unbounded recursion before validation. A reference implementation may reject files above a conservative documented byte bound, but must still catch `RecursionError` as invalid input rather than crash.

## Stable dispatch

Registry keys are `(rule_id, rule_version)`. Registration fails on duplicate keys. Selection is explicit through a local manifest mapping predicates to pinned rules. Zero matches produces a recorded unsupported-obligation result, normally insufficient evidence. Multiple matches require an explicit selection or a declared multi-check experiment; never pick whichever module imported first.

```python
def check_claims(
    context: CheckContext,
    claims: tuple[Claim, ...],
    plan: CheckPlan,
    registry: RuleRegistry,
) -> CheckRun:
    # Validate plan; evaluate by stable claim and rule identity.
    # Validate returned references and verdicts before accepting results.
    # Return findings and execution errors as separate collections.
    ...
```

Order work by claim ID and rule key. Parallel execution may finish in any order, but collection sorts results before final encoding. A plugin exception becomes an execution error tied to that invocation. Other independent claims may continue. A plan must declare whether an operational failure prevents overall completion; it must not be silently hidden by successful neighboring rules.

## Check context

Context exposes read-only indexed records and a bounded artifact reader that returns verified bytes. It also exposes explicit assumptions and profile versions. A rule cannot create evidence by referring to arbitrary IDs. Validate each returned finding: claim identity matches, rule identity matches the invoked version, evidence references resolve, assumptions are in scope, witness size is bounded, and the content-derived finding ID is correct.

A pure rule may still be computationally expensive. Use operation budgets in algorithms and subprocess-level resource limits for untrusted or expensive extensions. A deterministic budget counts work units, such as graph edges visited or candidate evaluations. Wall time is an operational protection and may stop different machines at different points; record that interruption rather than claiming deterministic completeness.

## Seeds and generated experiments

Never rely on module-global randomness. Pass a random source explicitly to generation code. To avoid depending on the evolution of a library PRNG, define a simple versioned deterministic byte stream for the reference fixtures: SHA-256 over a seed byte string, a domain separator, and an increasing unsigned counter. Consume fixed-width blocks. For bounded integers use rejection sampling, not biased modulo reduction. The generated fixture records generator version, seed, and consumed counter interval.

This construction is for reproducible test generation, not a claim of security-sensitive randomness. Candidate subjects may use a different explicit PRNG, but their resume state and algorithm identity must be part of the experiment contract. Choosing a seed from the current clock is permitted only in a convenience CLI option that immediately records it before execution.

## Semantic output versus observation metadata

Separate `result.json` from `run_metadata.json`. The former includes claim findings, witnesses, assumptions, and deterministic completion scope. The latter may include elapsed wall time, operating system, and diagnostic timestamps. Do not hash observation metadata into the semantic result identity. Conversely, configuration that changes semantics, such as a comparison tolerance or work budget, must be pinned in the plan and cannot be dismissed as metadata.

## Tests and completion criteria

Run canonical vectors in fresh processes with different `PYTHONHASHSEED` values. Permute object insertion order and set-like record arrays; identities must remain equal. Permute semantic operation arrays; identity must change. Execute the same check plan sequentially and with shuffled completion order; normative results must match byte for byte.

Test duplicate registration, absent rules, ambiguous selection, a rule returning another claim's ID, a fabricated evidence reference, mutation attempts against context, and rule exceptions. Include integer boundary cases and exact byte fixtures with non-ASCII text. Cross-implementation vectors should specify expected bytes as well as hashes so failures are diagnosable.

Acceptance requires deterministic results for all built-in rules, no implicit plugin loading, and no network or process creation on importing the core. Repeatability is an executable gate, not a sentence in the README. A change to canonical encoding or rule semantics requires a new version and migration notes rather than silently reinterpreting existing identities.

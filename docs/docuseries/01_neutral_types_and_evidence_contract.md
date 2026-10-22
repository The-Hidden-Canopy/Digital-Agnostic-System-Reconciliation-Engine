# 01 Neutral types and the evidence contract

## Purpose

Define a small versioned interchange contract before writing inference rules. The contract describes records and references; it does not prescribe a vendor's workflow or embed a domain's decision thresholds. Version one uses UTF-8 JSON and a directory of explicitly referenced artifact files. The same records are available through frozen Python dataclasses.

Implement `model.py`, `validation.py`, and `schema_v1.py`. The last module contains schema metadata and handwritten validators. A published JSON Schema can document the contract, but the reference implementation does not require a third-party schema engine. Parser behavior, reference resolution, and identity checks remain executable code with contract tests.

## Top-level bundle

Required top-level fields are `schema_version`, `bundle_id`, `artifacts`, `claims`, `activities`, `assumptions`, and `profile_refs`. All list fields must be present even when empty. Version one rejects unknown top-level fields so misspellings cannot quietly disappear. A future extension mechanism must be specified and versioned rather than silently ignoring arbitrary payloads.

An artifact contains `id`, `digest`, `media_type`, `size_bytes`, and `relative_path`. Its digest is SHA-256 over raw file bytes. Its record ID addresses its canonical metadata. Two paths containing the same bytes have the same content digest but can have different record IDs. File integrity does not establish the truth of the file's contents.

A claim contains `id`, `predicate`, `subject`, `arguments`, `evidence_refs`, and `assumption_refs`. Predicate names identify registered semantics. Subject is an opaque, nonempty identifier within this bundle, not a URL to be fetched. Arguments are bounded JSON values. Evidence references name artifact or activity IDs; assumption references name assumption IDs. Claims never contain an authoritative verdict supplied by the producer.

An activity contains `id`, `operation`, `inputs`, `outputs`, `depends_on`, `generation`, and `logical_clock`. Inputs and outputs reference artifacts. Dependencies reference activities. Generation is an opaque experiment-generation identity. Logical clock is either null or an object with `clock_id` and nonnegative integer `tick`. A clock does not establish real-world synchronization with any other clock.

An assumption contains `id`, `statement`, and `scope`. Statement is descriptive text, never executable code. Scope identifies the claim or experiment to which it applies. A profile reference contains `profile_id`, `profile_version`, and `digest`; it resolves only through an explicit local registry.

## Typed Python surface

```python
from dataclasses import dataclass
from enum import StrEnum
from typing import TypeAlias

JsonValue: TypeAlias = (
    str | int | bool | None | list['JsonValue'] | dict[str, 'JsonValue']
)

class Verdict(StrEnum):
    SUPPORTED = 'SUPPORTED'
    CONTRADICTED = 'CONTRADICTED'
    INSUFFICIENT_EVIDENCE = 'INSUFFICIENT_EVIDENCE'
    INVALID_EVIDENCE = 'INVALID_EVIDENCE'
    NOT_APPLICABLE = 'NOT_APPLICABLE'
    HUMAN_REVIEW_REQUIRED = 'HUMAN_REVIEW_REQUIRED'

@dataclass(frozen=True)
class Claim:
    id: str
    predicate: str
    subject: str
    arguments: JsonValue
    evidence_refs: tuple[str, ...]
    assumption_refs: tuple[str, ...]
```

Freezing a dataclass does not freeze nested dictionaries. Convert validated JSON into an immutable internal tree or retain canonical bytes and decode into private temporary values when evaluating. Do not expose mutable bundle dictionaries to rules. The public parser returns either a validated bundle or a collection of diagnostics, never a partially initialized bundle presented as valid.

## Findings and execution errors

A finding contains `id`, `claim_id`, `verdict`, `rule_id`, `rule_version`, `evidence_refs`, `assumption_refs`, `reason_code`, and `witness`. Witness is null or bounded structured JSON. Reason codes are stable identifiers; explanatory prose belongs in an optional report layer rather than identity-critical findings. Every referenced input must belong to the validated context.

An execution error contains an error code, operation identity, bounded diagnostic text, and optional retryability metadata. Do not serialize stack traces or environment variables by default. A runner's nonzero exit is not automatically a contradiction. For example, a missing executable is a laboratory failure; a valid run producing an invalid state may support a contradiction through a separate checking rule.

## Identity and canonical values

IDs use a type-prefixed hash, such as `claim:sha256:<hex>`. Hash canonical record content excluding only its own `id`. Bundle identity excludes only `bundle_id`, includes referenced record content, and uses a canonical ordering for top-level record collections. Record references are sorted where their meaning is a set; activity operation sequences remain ordered where order is semantic. Chapter 02 specifies the algorithm.

Normative records prohibit JSON floating-point numbers. Use integers for counts and clock ticks. Represent measured decimals as objects such as `{"kind":"decimal","value":"2.50","unit":"seconds"}` only where a predicate explicitly understands that representation. Decimal lexical normalization is defined by that predicate, not guessed by the global serializer. Reject NaN, infinity, and duplicate JSON keys at parsing time.

Preserve Unicode exactly; do not silently normalize identifiers. Reject unpaired surrogates that cannot encode as strict UTF-8. A display layer may escape confusable text, but must not change hashed content. Boolean values must not pass integer validators merely because Python treats `bool` as a subclass of `int`.

## Validation passes

Perform bounded byte parsing first, then recursive shape validation, record identity checking, reference resolution, and file verification. Return diagnostics with a JSON-pointer-like location and stable code. Sort diagnostics by location and code for reproducibility. Enforce configured limits on file size, nesting depth, array length, and string length before expensive graph analysis. Limits are public configuration values, not hidden domain policies.

Require unique record IDs. If an artifact file is absent, preserve the distinction between a structurally valid reference and unavailable content. Shape corruption is invalid evidence; unavailable required content is insufficient evidence at claim evaluation. A file whose actual digest disagrees with its declared digest is invalid evidence. Never fetch missing references automatically from the network.

Relative paths must stay inside the bundle root. Reject absolute paths, traversal components, and ambiguous separators; the package-integrity chapter defines race-resistant opening and symlink handling. Parsing a bundle must not import modules or execute a command mentioned in its content.

## Acceptance cases

Contract tests cover duplicate keys, repeated IDs, dangling references, unsupported versions, absent required fields, true where a count is expected, oversized nesting, malformed hashes, Unicode edge cases, and changed artifact bytes. Round-trip tests must preserve semantic identity. A finding that names evidence absent from its context must fail validation. A bundle with an unknown predicate remains structurally valid but cannot receive support from an unregistered checker. Publish positive and negative contract vectors so another implementation can reproduce these decisions without importing the reference library.

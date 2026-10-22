# 08. Secure evidence ingestion and cryptographic identity

## Status and purpose

This chapter is a proposed implementation, not a description of a completed or tested verifier. Source inspection established useful patterns: GLYPH distinguishes an incomplete journal tail from a corrupt complete record; STRATA separates authoritative content from disposable presentation state and verifies blobs incrementally. The implementation below is new, generic work. It does not import their formats, constants, fixtures, or code.

The ingestion boundary receives hostile bytes, not trusted objects. A syntactically valid bundle can still contain traversal paths, duplicate identifiers, a decompression bomb, misleading signature material, or claims that are unsupported by its attachments. Ingestion establishes which bytes were admitted and whether their declared structure is internally valid. It does not establish that a claim is true, a signer is authorized, or the supplied history is complete.

## Simple core, expandable boundary

Ship this boundary inside one local application. The first implementation needs only strict JSON admission, bounded copying and SHA-256 integrity; no daemon, cloud account, key-management service or mandatory signature system is needed. Signature adapters, archive readers and streamed-journal recovery are optional capabilities behind stable interfaces. Format extraction may use a short-lived isolated subprocess for hostile documents; that is a containment boundary, not a microservice architecture. Unsupported optional verification is reported explicitly when a selected rule requires it.


## Modules and contracts

Use the common `verifier_lab.model` records from Chapters 00–07: Bundle, Artifact, Claim, Activity, Finding and the shared Verdict enum. Add:

```python
# verifier_lab/ingestion/admit.py — illustrative signatures
@dataclass(frozen=True)
class IngestionLimits:
    max_manifest_bytes: int
    max_artifact_bytes: int
    max_total_bytes: int
    max_artifacts: int
    max_json_depth: int

@dataclass(frozen=True)
class AdmittedArtifact:
    artifact_id: str
    digest: str
    size_bytes: int
    private_copy: Path

def admit_bundle(source: Path, destination: Path,
                 limits: IngestionLimits) -> AdmittedBundle: ...

# verifier_lab/trust.py
def evaluate_attestation(attestation: Attestation,
                         anchors: TrustStore,
                         context: TrustContext) -> TrustFinding: ...
```

Limits are explicit deployment inputs with versioned profiles, not copied product thresholds. The reference release ships documented conservative example values; changing them changes the run configuration digest. A resource limit prevents processing and becomes an ExecutionError with a stable code, not a false finding against the underlying claim. A malformed or hash-mismatched evidence record is INVALID_EVIDENCE. Missing required artifacts are INSUFFICIENT_EVIDENCE when the package can still be interpreted; structurally impossible references can additionally produce a package-validation finding.

## Admission algorithm

Version one should accept a directory package and a bounded manifest file. Defer archive support until the same invariants have explicit tests. Do not execute a supplied script, load a pickle, dynamically import a Python module, evaluate a formula, or dereference a network URL found inside evidence. Content-type declarations are descriptive; they do not select arbitrary parsers.

1. Read the manifest with a byte bound before JSON decoding. Decode UTF-8 strictly. Reject duplicate object keys using an `object_pairs_hook`; ordinary `json.loads` silently accepts them.
2. Validate structural depth and count budgets, required fields, field types, known schema version, digest syntax and unique IDs. Canonical records forbid floats and non-finite numbers. Reject booleans where an exact integer is required, because Python treats `bool` as an `int` subclass.
3. Validate every artifact path as a relative package path. Reject absolute paths, parent components, drive-qualified paths, NULs and unsupported separator conventions. Reject symlinks and non-regular files. Cross-platform path policy must be explicit rather than relying on the current platform's path parser alone.
4. Open and copy each artifact into a newly created private staging directory while computing SHA-256 and counting bytes. Compare declared size and digest to the copied bytes. Verification subsequently reads only this admitted copy, not a mutable original path.
5. Validate all references against the admitted inventory. Keep missing, malformed and conflicting references distinct. Finalize admission by atomically installing a manifest of the admitted bytes.

```python
def copy_and_hash(source, sink, *, maximum, chunk_size):
    h = hashlib.sha256()
    count = 0
    while chunk := source.read(chunk_size):
        count += len(chunk)
        if count > maximum:
            raise ResourceLimit("artifact_bytes")
        h.update(chunk)
        sink.write(chunk)
    return "sha256:" + h.hexdigest(), count
```

This snippet is not a complete safe file opener. A check-then-open sequence can race. On supported POSIX systems, use directory-relative descriptors and no-follow flags, verify the opened descriptor with `fstat`, and reject unexpected file types. On platforms without equivalent protections, require a controlled local staging location and state the concurrency limitation. A portable standard-library implementation must not claim universal resistance to hostile concurrent filesystem mutation.

## Identity, canonicalization and signatures

A content digest establishes equality with the expected bytes. An attacker able to replace both content and its expected digest can produce a consistent forgery. Therefore distinguish four claims: byte integrity, publisher authenticity, authorization of the publisher for a purpose, and history completeness. None implies the others.

Use SHA-256 from `hashlib`, standard canonical JSON from Chapters 01–02, and domain-separated signing messages. An example proposed signing envelope binds the format version, canonical manifest digest, intended purpose and signer-key identifier. It must not include arbitrary executable verification instructions. The bundle ID is derived from the unsigned normative manifest, excluding its own ID and detachable signatures, to avoid a circular identity.

Python 3.12's standard library does not provide an Ed25519 implementation. Do not implement elliptic-curve cryptography in project code. Optional signatures require an explicitly selected, maintained cryptography dependency and a narrow verification adapter; the standard-library-only mode still supports content integrity and reports signature verification as unsupported when required. HMAC can be useful for a closed shared-secret context, but cannot provide independent public verification and should not be described as equivalent to a digital signature.

Trust anchors come from verifier configuration outside the package. A package-supplied key is not its own trust anchor. Key identity, accepted algorithm, purpose, validity interval and revocation knowledge are explicit inputs. Unknown revocation state must remain unknown. Historical verification distinguishes “valid under the declared historical trust snapshot” from “acceptable under current trust policy.” No bundled timestamp alone proves when a signature was made.

## Prefix recovery without evidence rewriting

For a future record-stream format, a parser may identify a valid prefix and an incomplete final record. It must preserve the original bytes and report the incomplete tail; it must not truncate evidence during verification. A corrupt complete record is different from a torn write. A terminal completion marker does not prove that all expected events exist unless an external expected count, commitment or trusted checkpoint binds completeness.

## Tests and acceptance

Generate synthetic cases for duplicate JSON keys, oversized manifests, deeply nested arrays, duplicate IDs, unknown algorithms, signature purpose mismatch, missing trust anchors, symlink paths, traversal paths, file growth during copy, and altered content with an unchanged digest. Test every truncation point of a generated stream. For each failure assert both classification and absence of source mutation.

Acceptance requires bounded-memory artifact hashing, deterministic admitted identities, no network access during normal verification, no loading of package code, and explicit unsupported-platform behavior. Verify the distinction between a valid signature by an untrusted key and a trusted signature. The chapter is complete only after those tests run on the supported platform matrix; this document claims no such execution.

## Pattern provenance

- [GLYPH validated journal parsing and recovery](https://github.com/The-Hidden-Canopy/GLYPH/blob/d0a51c68c45f498edd9b8e22a5e68cb673a64c70/src/transport/resume_journal.cpp).
- [STRATA streaming content verification](https://github.com/The-Hidden-Canopy/STRATA/blob/49345022a36b2465ecf04f32fc11557d3b718266/strata/blobs.py).

These references explain the observed design patterns. They are not authority to transplant source or to claim the proposed implementation already works.

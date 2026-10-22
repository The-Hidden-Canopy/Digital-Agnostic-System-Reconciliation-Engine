# 17 Document Ingestion and Reconciliation

## Purpose and trust boundary

Document ingestion turns immutable bytes into reviewable evidence and candidate claims. It does not turn documents into truth. A source can be authentic yet mistaken, current yet irrelevant, or perfectly parsed yet internally contradictory. Preserve these distinctions from the first import through the final report.

The required input formats are PDF, JSON, Markdown, and SQL files. SQL support means static, dialect-aware parsing, not connecting to a database or executing statements. No database connection is required or authorized by this design. Deterministic structured extraction is the default. An optional model adapter can suggest interpretations, but no AI provider is mandatory and suggestions remain untrusted, potentially nondeterministic inputs to human review.

Keep one application and a small shared core: an adapter registry selects installed capabilities, local storage holds durable attempts, and a bounded subprocess handles unsafe parsing. No message broker, orchestration service, account, or model is required. Advanced configuration stays behind the simple Import -> Review claims -> Run checks -> Findings -> Export path.

Build this as an isolated layer above the byte-admission boundary and below the immutable verification core. Use Python 3.12 with explicit optional parser adapters. The module names and contracts below are proposed implementation work, not installed capabilities or a completed parser.

```text
src/verifier_lab/ingestion/{admit,limits,worker,coverage}.py
src/verifier_lab/ingestion/adapters/{pdf,json,markdown,sql}.py
src/verifier_lab/ingestion/{models,spans,extract,reconcile,compile}.py
src/verifier_lab/ingestion/suggestions/{protocol,disabled}.py
src/verifier_lab/review/{commands,history,invalidation}.py
schemas/ingestion/{source-document,source-span,claim-draft}.json
fixtures/ingestion/{pdf,json,markdown,sql,hostile}/
```

## Data model and core compatibility

Keep ingestion records in a versioned application schema. Do not add undocumented fields to Bundle v1. The core Bundle retains schema_version, bundle_id, artifacts, claims, activities, assumptions, and profile_refs. Its Artifact has id, digest, media_type, size_bytes, and relative_path. Its Claim has id, predicate, subject, arguments, evidence_refs, and assumption_refs. Compile additional ingestion metadata into ordinary provenance artifacts referenced from those existing fields.

```python
@dataclass(frozen=True)
class SourceDocument:
    id: str
    artifact_id: str
    source_digest: str
    detected_format: str
    declared_dialect: str | None
    extraction_manifest_id: str
    coverage_manifest_id: str

@dataclass(frozen=True)
class SourceSpan:
    id: str
    source_artifact_id: str
    rendition_artifact_id: str
    locator: dict
    text_digest: str
    extraction_method: str
    confidence_decimal: str | None

@dataclass(frozen=True)
class ClaimCandidate:
    id: str
    proposed_predicate: dict
    source_span_ids: tuple[str, ...]
    extraction_manifest_id: str
    unresolved_qualifiers: tuple[str, ...]

@dataclass(frozen=True)
class ClaimDraft:
    id: str
    candidate_id: str | None
    predicate: dict
    source_span_ids: tuple[str, ...]
    assumption_ids: tuple[str, ...]
    review_decision_id: str
```

A candidate is unreviewed. A draft is a reviewed interpretation awaiting core validation and commit. A committed claim is immutable and receives a core Claim identifier. Human approval means “this expresses the proposition to test,” not “this proposition is true.” Each edit creates a new decision and draft revision; retain rejected candidates and the reasons for rejection according to the project's retention policy.

Every extraction manifest records source digest, parser name and version, adapter version, configuration, declared dialect, resource limits, locale assumptions, and optional OCR details. Span identity binds the source digest, rendition digest, extraction-manifest digest, and canonical locator. Use canonical JSON with sorted keys, compact UTF-8 encoding, and no floats or NaN for hashed objects. IDs exclude their own ID field from the hashed payload. Exact numeric representations are strings with explicit tags. A parser upgrade creates a different rendition even if some displayed text is unchanged.

## Admission and hostile-file handling

Accept bytes through the existing bounded admission mechanism. Store originals under generated content-addressed names; retain the submitted filename only as inert metadata. Never join user filenames into storage paths. Compare detected structure with declared type, reject unsupported formats explicitly, and quarantine parse failures without losing the admission record.

Run complex parsers in restricted subprocesses without network, shell execution, credentials, or access to unrelated workspace files. Apply byte, page, token, object, nesting, decompressed-size, memory, CPU, and wall-clock limits. A timeout is a parse diagnostic, not a negative finding about a domain claim. Make limits configurable through trusted project settings, not through instructions embedded in a file.

Archives are not a required import format. Reject them by default. If a later packaging feature accepts archives, use a separate bounded admission stage: reject absolute paths, traversal, symlinks, hardlinks, special files, duplicate normalized paths, excessive expansion, and nested archives unless explicitly supported. PDF compressed streams need expansion budgets even when no ZIP archive exists. Do not unpack embedded PDF attachments automatically.

All content, including comments, metadata, and apparently administrative instructions, is evidence data. “Ignore earlier instructions,” “run this SQL,” and “fetch this source” have no operational authority. Optional language-model prompts must isolate source text, with no tool capability attached; the real boundary is lack of execution authority, not a clever delimiter. Rendering must not activate scripts, macros, hyperlinks, or remote resources.

## Coverage is a first-class output

Create a coverage ledger for every admitted file, including completely unreadable ones. Each region is assigned PARSED, PARTIALLY_PARSED, UNSUPPORTED, CORRUPT, ENCRYPTED, RESOURCE_LIMIT, or NOT_ATTEMPTED, plus reason codes and parser diagnostics. These are ingestion states, not the six verification verdicts. A file-level summary is derived from its region ledger, not hand-assigned as success.

Coverage units depend on format: PDF pages and text/image regions; JSON lexical intervals and structural nodes; Markdown byte intervals and syntax nodes; SQL statements and their token ranges. Preserve byte-level accounting for text formats, including whitespace and comments as recognized nonclaim regions. PDF binary bytes are not a meaningful denominator for semantic coverage: report page and region coverage separately from binary object handling. Unknown extent must remain unknown rather than being counted as fully processed.

Coverage has two dimensions: syntax/content extraction and claim interpretation. Parsing every SQL token does not mean understanding every statement's semantics. Report “statement parsed, semantics unsupported” when appropriate. A claim that needs an unread region cannot be supported merely because another region parsed successfully. A query over only known rows must not establish a universal claim about an incompletely ingested dataset.

## PDF adapter

Extract native text with page and geometric provenance. Preserve text order as an extraction choice, not a guaranteed reading order: columns, tables, footnotes, and rotated text require explicit handling. Keep original page renderings and a normalized text rendition with mappings back to page coordinates. Normalize coordinate units and record crop, rotation, origin, and transform metadata. A SourceSpan can use page index, bounding boxes, and rendition character offsets; document clearly that displayed page numbers are one-based while stored indexes are zero-based.

Detect image-only or suspiciously sparse pages and mark the missing text. Optional local OCR is a separately selected adapter with language, engine version, configuration, page-image digest, and confidence information. Native text and OCR are separate renditions, not silently merged strings. OCR confidence is extractor metadata and cannot be substituted for evidence reliability. Low-confidence numbers, negation, dates, and units should trigger explicit review even if the surrounding words appear readable.

Password-protected documents report ENCRYPTED without password guessing. Broken cross-reference tables, unsupported fonts, malformed objects, and timed-out rendering retain exact diagnostics. Tables should retain cell and page mappings; if structure is uncertain, expose plain spans and require review rather than inventing row relationships. Never treat a PDF's digital-signature graphic as cryptographic verification. Authenticity checking, if implemented, is a distinct adapter with a stated trust model.

## JSON adapter

Use a parser that retains lexical offsets and ordered object member occurrences. A conventional dictionary parse can discard duplicate keys before the system notices them. Default behavior rejects duplicate keys for semantic compilation while preserving both occurrences and their spans. A project may select a documented interpretation policy, but that policy must be visible, versioned, and an explicit assumption, never accidental last-key-wins behavior.

Preserve integer and decimal tokens exactly. Do not round large integers through JavaScript numbers or Python floats. Distinguish an exact numeric value from its lexical presentation when representation matters: 1, 1.0, and 1e0 may compare numerically equal while remaining different source tokens. Define negative zero, exponent limits, and leading-zero rejection according to the supported grammar. Reject NaN and Infinity in the strict JSON profile.

Limit document depth, string length, token count, and numeric digit count before expensive conversions. Validate optional schemas only from pinned local resources; remote schema references are disabled. Record schema version, digest, validation outcome, and failing pointers. Schema validity proves shape constraints, not truth. JSON Pointer alone cannot identify a duplicate member, so add member occurrence index and byte range to locators. Any repair of malformed JSON creates a separate proposed derivative, never silently replaces the original.

## Markdown adapter

Parse Markdown into inert syntax nodes while preserving UTF-8 byte offsets and line/column mappings. Pin the supported grammar and extensions in the extraction manifest. Preserve headings, paragraphs, lists, tables when supported, code fences, links, and raw HTML as source data. Front matter is an optional recognized region with its own bounded parser and explicit format.

Do not fetch linked images, URLs, includes, transclusions, or remote code snippets. Do not execute code fences, embedded scripts, template directives, or HTML. A reference to an external document can become an unresolved evidence reference, not imported authority. Display links as text or deliberate navigation controls, never background fetches. If an extension is unsupported, account for its original region and mark its interpretation unsupported.

Deterministic extraction can propose claims from a specified front-matter schema or well-defined tables. Free prose produces bounded suggestions or remains unstructured evidence. Heading proximity is a heuristic context clue, not permission to apply a heading's date or jurisdiction to every nested sentence without review.

## SQL adapter

Require a declared dialect or an explicit unresolved-dialect state. Sniffing can suggest a dialect for review, but cannot silently decide semantics. Pin parser and grammar versions. Parse only; never call a database driver, execute statements, evaluate stored routines, follow client include commands, or resolve URLs. Imported SQL cannot change the project's database or filesystem.

Classify statements as schema definition, data assertion, query definition, transaction/session configuration, or unsupported procedural/client content. A CREATE TABLE is evidence of a declared schema, not proof that any deployed database has that schema. INSERT literals describe asserted data in the file, not necessarily current production rows. A SELECT describes a query, not its result. Triggers, dynamic SQL, dialect-specific commands, collation, timezone, NULL semantics, and implicit casts need explicit support or an unsupported-semantic marker.

Retain comments and token positions. Comments may contain intended constraints but must not override parsed syntax; represent them as separate textual assertions. Do not split statements by semicolons without a lexical parser because quoted strings, comments, and procedural bodies can contain semicolons. Handle SQL dumps with bulk-data sections only through an explicitly supported bounded grammar; otherwise preserve and flag those regions. Identifier case folding and quoted identifiers follow the declared dialect. Do not normalize away distinctions that affect object identity.

A safe candidate might assert that a file declares a non-null column. An unsafe candidate would assert that a business system never stores nulls merely from that declaration. Keep the subject scoped to the artifact unless independently supported deployment evidence is available.

## Extraction and reconciliation algorithms

Deterministic extractors emit typed predicates only from supported structures, with source spans, units, scope, time interval, and unresolved qualifiers. Optional model-assisted suggestions record the adapter, model identity when available, prompt-template digest, configuration, output bytes, and nondeterministic status. Sending private content to an external provider requires a separately authorized adapter and data-sharing decision. A seed alone is not a reproducibility guarantee. Replaying a stored suggestion is different from reproducing its generation.

Reconciliation proposes relationships without deleting evidence:

```python
for candidate in stable_order(candidates):
    validate_shape_and_span_bindings(candidate)
    key = semantic_key(candidate, preserve_scope_time_units=True)
    for prior in lookup_compatible_scope(key):
        if exact_predicate_equal(candidate, prior):
            propose("DUPLICATE", candidate, prior)
        elif intervals_overlap(candidate, prior) and mutually_exclusive(candidate, prior):
            propose("CONTRADICTION", candidate, prior)
        elif explicit_authority_and_replacement(candidate, prior):
            propose("SUPERSESSION", candidate, prior)
    enqueue_human_review(candidate)
```

Semantic keys include subject identity, predicate, jurisdiction or domain scope, units, quantification, and effective interval. Similar wording is not enough for deduplication. A later publication date alone does not prove supersession. Contradiction detection must distinguish “not stated” from an explicit negative and distinguish incompatible facts from different time windows. Unit conversions require exact rules and provenance. Ambiguous relationships remain proposals requiring review.

Maintain dependency edges from claims to spans, extraction manifests, assumptions, policies, and upstream claims. On change, traverse reverse edges with a visited set; mark affected current projections stale and record the triggering change. Detect strongly connected components so cycles cannot create infinite invalidation or circular proof. Preserve historical runs and their exact snapshots.

## Transactions, errors, tests, and release stages

The import job follows ADMITTED -> PARSING -> EXTRACTED -> REVIEW_PENDING -> COMPILED, with explicit rejected, partial, failed, and cancelled outcomes. It runs inside the durable job lifecycle described in Chapter 16. Commit source admission independently from extraction so a crashed parser does not erase knowledge of the file. Publish each rendition, span manifest, coverage ledger, and candidate set atomically as one extraction attempt; retries create attempts rather than mixing versions.

The compile command requires approved decisions, valid span references, and a pinned extraction manifest. It emits provenance artifacts, core claims, and activities into an immutable Bundle. Reject dangling references and mismatched digests before evaluation. Verification outcomes remain SUPPORTED, CONTRADICTED, INSUFFICIENT_EVIDENCE, INVALID_EVIDENCE, NOT_APPLICABLE, or HUMAN_REVIEW_REQUIRED; operational failures remain separate typed errors.

Implement in stages: first byte admission and coverage contracts; then strict JSON and inert Markdown; then static SQL for a deliberately narrow declared dialect set; then native PDF extraction; then optional OCR; finally model suggestions and reconciliation assistance. Required format support does not mean every dialect, damaged file, or PDF feature is supported. Publish the capability matrix and test it.

Acceptance fixtures include duplicate JSON keys, integers beyond binary64 precision, deep nesting, malformed UTF-8, Markdown script and include attempts, SQL comments and quoted semicolons, unsupported procedural syntax, image-only and mixed PDF pages, encrypted PDFs, huge compressed streams, forged extensions, traversal names, and cancellation during parsing. Tests assert no network or database calls, bounded resource use, reproducible deterministic output, exact span recovery, complete coverage accounting, and stable failure codes. A clean-room export verifier must detect missing provenance and altered originals without needing the producing UI. The end-to-end gate is simple: every compiled claim can be traced to immutable bytes and a recorded review decision, while every unread or uninterpreted region remains visible.

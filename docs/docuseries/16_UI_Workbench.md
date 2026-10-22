# 16 UI Workbench

## Purpose and implementation boundary

Build a local-first workbench that lets a reviewer follow a conclusion back to its evidence, assumptions, executable checks, and revisions. The interface is not a compliance certification product. It must never turn a green test result into an unqualified claim that an organization or system is compliant. Its promise is narrower: make a scoped verification process inspectable and repeatable.

This chapter proposes a TypeScript and React frontend backed by the Python 3.12 reference core through a versioned local HTTP API. These are implementation specifications, not claims that the application, packages, or deployment already exist. Keep the default product to one local application, one shared core, and local durable storage. No distributed service, account, model provider, or separate worker fleet is compulsory. The headless CLI remains a first-class client. Neither client owns verification semantics; both submit the same immutable inputs to the same core. Vendor-specific adapters and optional model suggestions sit behind explicit boundaries, not inside UI components.

Suggested source layout:

```text
apps/workbench/src/routes/{projects,sources,claims,runs,risks,reports}/
apps/workbench/src/components/{EvidenceViewer,GraphTable,JobTray}/
apps/workbench/src/contracts/generated.ts
apps/workbench/src/state/{jobProjection,reviewDrafts}.ts
src/verifier_lab/api/{app,routes,schemas,events}.py
src/verifier_lab/application/{commands,queries,authorization}.py
src/verifier_lab/jobs/{runner,journal,cancellation}.py
src/verifier_lab/reports/{manifest,export,verify_bundle}.py
```

The API binds to loopback by default. Local does not mean trusted: validate Host and Origin, reject wildcard cross-origin access, use a per-session capability and mutation protection, and do not put credentials in URLs. A browser tab must not be able to cause an unrelated local service to import files. If multi-user or remote deployment is added, authentication, authorization, tenant isolation, and deployment hardening become a separate release gate. Do not present a localhost prototype as a multi-tenant service.

## Screen model and reviewer journey

The default navigation is exactly five steps: **Import -> Review claims -> Run checks -> Findings -> Export**. A beginner can finish a useful verification without opening a graph, configuring a jurisdiction pack, or learning an experiment system. The screens described below are route and component boundaries, not ten mandatory steps. Source details and claim details open as nested views; project summary is a compact home panel; temporal replay, dependency graphs, risk reviews, and advanced run parameters live behind labeled drawers or an Advanced workspace. Keep advanced controls discoverable without crowding the basic path.

The project overview displays a named scope, source inventory, claim counts by verdict, unresolved review items, and the exact current snapshot. Show stale results separately from current ones. Counts must include insufficient evidence and human review requirements, rather than calculating a misleading percentage from only completed passing checks. Each summary opens a filtered list whose denominator is visible.

The Sources screen accepts PDF, JSON, Markdown, and SQL uploads. Before import, show file size, detected type, declared SQL dialect where applicable, parsing limits, and whether an optional OCR or model adapter will run. The default is deterministic local extraction with no remote content retrieval. After import, show per-file and per-region coverage, not just a single success toast. A partly readable PDF is not a fully parsed source. Files with unsupported dialects, corruption, encrypted pages, or resource limits remain visible with actionable diagnostics.

The Candidate Review screen is a two-pane inbox. The left pane contains a proposed assertion, extraction method, structured predicate, qualifiers, effective interval, and candidate confidence if an extractor supplies one. The right pane opens immutable source spans. Confidence in extraction is not confidence in truth. Actions are approve as a claim draft, edit and approve, reject, or defer. Editing changes the interpretation, never the source. A reviewer must supply a reason when changing scope, units, negation, or dates because those changes can reverse meaning. Bulk approval is limited to explicit compatible selections with a preview of the exact resulting revisions; it must not silently approve hidden rows.

The Claim Detail screen contains the proposition and its revision history; evidence for and against it; dependencies and assumptions; the latest run and older runs; owner and disposition; and outstanding review tasks. A run records a verdict for a specific claim revision and evidence snapshot, not for an eternally mutable claim name. The interface uses exactly these verdict labels:

```text
SUPPORTED
CONTRADICTED
INSUFFICIENT_EVIDENCE
INVALID_EVIDENCE
NOT_APPLICABLE
HUMAN_REVIEW_REQUIRED
```

Execution errors occupy an adjacent error panel and never masquerade as one of those verdicts. For example, a killed worker is a failed job with no completed verdict, not evidence that a claim is contradicted. NOT_APPLICABLE requires an explicit applicability decision and scope rationale. A supported result displays its assumptions and evaluated interval beside the label.

The Run Builder selects claim revisions, evidence snapshot, domain pack, policy version, temporal bounds, and deterministic execution settings. Its confirmation view previews missing prerequisites and the number of affected claims. Start returns a job identifier immediately. The Run Detail view separates queue time, parsing, evaluation, and export stages; shows bounded logs with safe rendering; and preserves the input manifest even after cancellation.

The Findings screen groups observations without losing their individual provenance. A finding links a risk, control objective, executable check, evidence, affected claim revision, and run. Ownership and remediation disposition do not change the evidence verdict. Closing a ticket is not proof that a control works. A remediation creates a new version and schedules or requests re-evaluation; the old finding remains inspectable.

## Evidence viewing and graph navigation

EvidenceViewer uses artifact hashes and span identifiers rather than arbitrary filesystem paths. PDF views expose page number, bounding boxes, text-layer or OCR origin, and an accessible extracted-text alternative. JSON views highlight the lexical token range and pointer, including duplicate-key occurrence identifiers. Markdown views show inert source text plus safe structural presentation. SQL views highlight statements and comments while displaying dialect and parse coverage. Source strings are rendered as text, not executable markup.

A citation click opens the precise immutable rendition used by that run. If the user opens a later rendition, the interface visibly distinguishes it and offers a diff; it must not silently move an old citation onto different text. A span unavailable because the source was intentionally excluded from an export remains an explicit unavailable-evidence reference, not a broken blank pane. Search results display whether they search originals, extracted text, or both.

The dependency view offers both a visual graph and a fully functional table. Edges are typed: supports, contradicts, depends_on, derived_from, supersedes, or duplicates_candidate. Do not encode all relationships as generic arrows. Selecting a node opens its rationale and source links. Large graphs use server-side neighborhood queries and stable pagination; the UI labels omitted nodes and never implies that a truncated view is the whole graph. Cycles are shown as reviewable components, not erased by a layout algorithm. Keyboard users can traverse the table and inspect exactly the same information as graph users.

Temporal replay chooses both valid time and knowledge time where the model supports them. “What applied on June 1?” differs from “What did we know on June 1?” The replay header displays both, along with policy and source revisions. Counterexamples show the failing predicate, minimal witness when available, concrete variable bindings, trace steps, and reduction limits. A minimized counterexample is not claimed minimal unless the reduction procedure establishes that property. Original witnesses remain available to diagnose lossy reductions.

## Frontend and backend contract

Application-layer candidate, draft, span, review, and revision views compile into the existing verifier_lab model.py contracts. A committed claim revision maps to a core Claim id; ingestion provenance is stored as ordinary Artifact records and referenced through evidence_refs. Do not add UI-only fields to Bundle v1.

Generate TypeScript types from the versioned API schema and test compatibility in CI. Avoid hand-maintained duplicate enum definitions. All timestamps include timezone offsets; identifiers are opaque; hashes state their algorithm; numeric values requiring exactness cross the JavaScript boundary as tagged decimal strings, not binary floating-point numbers.

```typescript
type ClaimRunView = {
  runId: string;
  claimRevisionId: string;
  inputManifestHash: string;
  verdict: Verdict | null;
  freshness: "CURRENT" | "STALE" | "UNKNOWN";
  evaluatedAt: string | null;
  evidenceSpanIds: string[];
  assumptions: string[];
  errors: {code: string; message: string; retryable: boolean}[];
};

type SpanView = {
  spanId: string;
  artifactHash: string;
  renditionHash: string;
  locator: Record<string, unknown>;
  text: string;
  extractionMethod: string;
  extractionConfidence: string | null;
};
```

Example operations:

```http
POST /api/v1/projects/p1/runs
Idempotency-Key: client-generated-random-value
Content-Type: application/json

{"claim_revision_ids":["cr17"],"snapshot_id":"s4",
 "policy_revision":"pr3","as_of":"2026-10-01T00:00:00Z"}

202 Accepted
{"job_id":"j9","run_id":"r8","events_url":"/api/v1/jobs/j9/events"}

POST /api/v1/candidates/c7/decisions
If-Match: "candidate-revision-2"

{"action":"APPROVE_WITH_EDIT","predicate":{"op":"eq","lhs":"capacity","rhs":{"decimal":"100.00","unit":"kg"}},
 "reason":"Preserve the explicit kilogram qualifier"}
```

The server validates each command, authorizes its project scope, persists the decision and event atomically, and returns a revision. A stale If-Match returns 409 or 412 consistently with the published schema. The client keeps the unsaved draft and opens a comparison; it never overwrites the other review. Reusing an idempotency key with a different payload is an error. Repeating the same payload returns the original command outcome.

## Jobs, cancellation, and reconnection

Use a durable job state machine:

```text
QUEUED -> RUNNING -> SUCCEEDED
QUEUED -> CANCELLED
RUNNING -> CANCELLING -> CANCELLED
RUNNING -> FAILED
CANCELLING -> FAILED  [cleanup failure, with diagnostic]
```

A terminal success that commits before a cancellation request wins the race; cancellation then returns the terminal state without pretending to undo the run. Cancellation is cooperative between bounded units. Force termination after a grace period records an interrupted diagnostic and discards uncommitted output. Publish results with an atomic commit marker only after validation. Partial logs and completed subchecks may be retained but must be labeled partial and excluded from complete-run summaries.

Progress includes stage, completed units, total units if known, and last heartbeat. Do not invent a percentage when the denominator is unknown. A document parser can report “12 pages processed, total unknown” rather than 95 percent forever. Closing a tab does not cancel a job; the job tray says so. Cancel requires a distinct labeled control, not an ambiguous dialog close icon.

Events carry a monotonically increasing per-job sequence number. The event stream supports resume from the last acknowledged sequence. The client deduplicates replayed events, detects gaps, and reloads the authoritative snapshot when retention has expired. It must not infer completion from a disconnected stream.

```python
def apply_event(view, event):
    if event.seq <= view.last_seq:
        return view
    if event.seq != view.last_seq + 1:
        raise SnapshotRefreshRequired()
    return reduce_validated_transition(view, event)
```

Network loss displays reconnecting with the last confirmed timestamp. Refreshing the page retrieves the job state before subscribing again. Polling is an allowed fallback with bounded backoff. A backend restart recovers queued work and marks abandoned leases explicitly; it does not launch duplicate executions without checking durable attempt identifiers.

## Jurisdiction and risk review

A jurisdiction panel records the geographical scope, organizational activity, applicability facts, source authority, source edition, effective dates, and reviewer rationale. It distinguishes a machine-evaluable rule from an interpretive question requiring qualified human review. Ambiguous conflicts, missing facts, and uncertain applicability remain visible. The product can organize a review; it does not infer professional legal approval from a document match.

Maintain a navigable chain: risk -> control objective -> implementation assertion -> test -> run -> evidence spans. Many-to-many links are valid, but every edge needs a reason and revision. A risk can remain open despite several supported implementation assertions. User-entered acceptance decisions store owner, rationale, expiry or review date, and history. They cannot rewrite test results or remove counterevidence.

Changing source bytes, extraction settings, a claim, a dependency, a policy, or a checker creates an invalidation event. Reverse-dependency traversal marks affected latest views stale. The original run is immutable and still valid as a historical statement about its original inputs. The UI shows the invalidation cause and lets the reviewer compare or rerun, rather than overwriting yesterday's conclusion.

## Accessibility, failure states, and exports

Every screen must support keyboard-only use, visible focus, semantic headings, labeled inputs, skip navigation, and focus restoration after dialogs. Status changes use restrained live announcements; streaming every log line into a screen reader is unusable. Verdicts have text and icons, not color alone. Reduced-motion preferences disable animated graph transitions. Validation errors link to the affected fields and preserve entered content. Empty, loading, disconnected, permission-denied, corrupted, stale, and partially parsed states need distinct copy and fixtures.

Exports are generated from a pinned snapshot, never whatever happens to be on screen when generation finishes. Offer a human report and a machine bundle with source hashes, included originals or explicit omissions, normalized renditions, claim revisions, decisions, rules, checks, result records, counterexamples, environment manifest, and verification instructions. The report states scope, limitations, coverage gaps, and unresolved reviews. Excluding confidential originals reduces independent verifiability and must be disclosed before export. A hash manifest proves internal byte consistency, not trustworthy authorship or factual truth; signatures require separately managed trust anchors.

## Build stages and acceptance gates

1. Build the five-step shell and read-only project, source, claim, and run views against schema fixtures. Keep advanced views optional. Prove deep links and evidence spans survive refresh.
2. Add uploads and candidate decisions with optimistic concurrency and immutable histories. Prove edited interpretations do not alter originals.
3. Add durable jobs, event replay, cancellation, and stale-result invalidation. Inject process death and disconnects at each commit boundary.
4. Add graph, temporal replay, risk review, and counterexample views with equivalent accessible tables.
5. Add deterministic export and a clean-room offline verifier. A recipient must detect a modified byte, missing artifact, unresolved reference, and unsupported checker version.

Test the full journey with one artifact of each required type, contradictory claims, an unsupported SQL construct, an OCR gap, and a cancelled run. Contract tests reject unknown verdicts and unsafe numeric coercion. Browser tests cover repeated clicks, Back/Forward, refresh during work, close versus cancel, stale decisions, hostile source HTML, keyboard paths, and reduced motion. Acceptance requires that every displayed conclusion opens its exact inputs, no incomplete job appears successful, and no review disposition is represented as a compliance badge. These are release criteria to implement and test, not assertions that tests have already passed.

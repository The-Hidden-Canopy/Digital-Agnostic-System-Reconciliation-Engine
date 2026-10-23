# Build progress against the docuseries

Assessment: 22 October 2026. Baseline: `docs/docuseries/`, added in `cf1be07`.
This is a working narrow implementation with substantial advanced modules. It is not the full
laboratory or a reviewed public release. Chapter 13 explicitly allows optional jurisdiction packs
and advanced breadth to follow a usable generic release.

| Build area | Current implementation and evidence | Remaining scope |
|---|---|---|
| Contracts and deterministic dispatch (01–02) | `model.py`, `canonical.py`, `validation.py`, `engine.py`; contract vectors, hash-seed and adversarial tests | Broader platform evidence; additional rule families need their own witness contracts |
| Composition (03) | `composition.py`, `graph.py`; cycle/closure/tag/unit obligations, targeted scope, change-impact paths, independent checker, UI | Skipping evaluation using `evaluation_key` (keys are reported, checker-verified and compared across runs, but evaluation always re-runs); wider constraint vocabulary |
| Purpose-relative completeness (04) | `purposes.py`; role binding, generation coherence, lifecycle obligations, UI/export/checker | Richer coherence rules; user profile editing (conditional applicability is built: see FORMATS.md) |
| Stateful experiments and reduction (05, 07) | Independent reference/candidate KV store, transformation pipeline and seeded scheduler; three-way replay, KV crash enumeration, bounded reduction and fresh-process verification; external KV subject adapter | Broader stateful-system coverage; isolation of explicitly supplied external programs remains separate |
| Temporal reasoning (06) | Exact decimal interval reasoning and `interval.relation`; independent re-derivation | Richer temporal UI and additional rule families |
| Admission and document review (08, 17) | PDF/JSON/Markdown/SQL, required Linux namespaces/syscall boundary, subprocess limits, coverage, exact spans, append-only review, stale runs; adversarial and workflow suites | Optional OCR, trusted signatures, directory-package admission is outside the current upload path |
| Campaigns and independent reports (09–10) | Fixed mutation/metamorphic manifests, defective controls, independent totals/witness checking, external verifier adapter | Broader mutation/rule coverage and independently implemented language/runtime checker |
| Generic risk packs (11) | Strict data-only grammar, three-valued applicability, dependency/version checks, UI obligations and proposed remediation | Catalog breadth; reviewed jurisdiction source mappings |
| Packaging and release evidence (12, chapter 13 K) | `tools/check_release.py`: hashed Python build/parser inputs, two matching clean wheel builds, exact content allowlist, isolated offline installs, four-format CLI and optional installed-wheel Chromium journey; privileged Linux/Python 3.12 CI passed on 2026-10-23 | Broader platform matrix; full provenance/license review and owner release review |
| Workbench (16) | Shared backend, default five-step workflow, job cancellation/recovery, findings/spans, advanced composition/purpose/campaign pages, four-format browser journey | Screen-reader accessibility audit; broader browser coverage beyond the API-failure journey |
| Jurisdiction packs (14–15) | Design/source baselines only; no shipped jurisdiction pack | Official-source review, date/applicability rules, explicit incomplete coverage and qualified human review before shipping |

## Continuation delivered

The preceding eight increments already implement replay, reduction and campaigns; the old status
footer incorrectly listed those as absent. The next increment strengthens the narrow release path
rather than adding unreviewed regulatory content:

- Build two wheels from separate source copies under the same pinned Python build toolchain and
  deterministic timestamp; compare actual artifact hashes.
- Reject missing UI/catalog files, extra package files, duplicate/unsafe entries, symlinks and
  known local-path/private-key markers. This is a bounded package scan, not a general secret detector.
- Install the wheel into a fresh virtualenv outside the checkout; verify the core has no optional
  PDF/build-backend dependency, then install the pinned PDF extra from the local wheelhouse.
- Exercise Markdown/JSON/SQL claims and a reviewed PDF claim through CLI checking, export and
  independent package verification. Serve and load installed UI assets; optionally execute the
  complete browser journey, including PDF partial coverage, review, contradiction and export.
- Keep source-input digests, dependency-wheel digests, actual check outcomes and logs with the
  wheel. Record dirty source state; never attribute uncommitted work to the base commit alone.

See [RELEASE.md](RELEASE.md) for the reproducible commands and evidence boundaries. Existing
unit/contract/adversarial/integration tests remain a separate required gate; the release helper does
not substitute a smoke test for that suite. No files have been published or signed.

## Source-linked authoring continuation

The shared service, HTTP API and CLI now create pending human interpretations from exact UTF-8
rendition spans. The source view provides read-only passage selection and an equality-claim form.
Approval stays a separate action. The independent checker binds origin spans to their immutable
extraction and records that semantic entailment is not machine-verified. Extraction facts and
coverage remain unchanged; authoring cannot manufacture evidence. Tests cover Unicode boundaries,
invalid/stale selections, idempotency, persistence, source replacement, HTTP authorization,
non-equality built-in predicates through the service/CLI, export verification and provenance tampering.
The browser journey exercises the new flow against an installed wheel.

## Extraction retry continuation

An explicit re-extraction now always creates a separate, auditable attempt rather
than being treated as a duplicate upload. The source projection preserves the last
usable extraction for claims and provenance while separately exposing the latest
attempt and any operational error, so a failed retry cannot be hidden by older
successful output. The shared service, CLI (`reextract`) and workbench now offer
the path for every admitted format; SQL retries retain the prior declared dialect
unless the user chooses another one.

Focused retry regression checks passed under explicitly selected portable
development configuration on the recovered Windows workspace and in the
required Linux/Bubblewrap CI suite. The local Windows environment correctly
fails closed in required parser mode.

## Full laboratory continuation

All three synthetic systems named by chapter 00 now have independent reference/candidate transitions,
positive controls, intentional defects, full-state three-way comparison and reduced reproducer
verification in a fresh process. The pipeline detects stale transformation-cache reuse and lost source
revisions; the scheduler detects unstable deadline ties and lost random continuity. The CLI, bounded
HTTP endpoints and Advanced workbench expose both, including counterexample download. Randomized
reference-agreement tests include rejected operations, and forged witnesses/signatures remain rejected.
The installed-wheel acceptance also exercises both systems and their reduced counterexamples.

Local validation for this increment: 361 Python tests passed; the expanded installed-wheel Chromium
journey passed; two independent wheel builds matched. These are Linux/Python 3.12 observations,
not a claim that the GitHub workflow or other platforms have executed.

## Parser isolation continuation

Required Linux parser mode now uses a minimal read-only Bubblewrap filesystem, private process
and network namespaces, libseccomp native syscall denial, empty environment and bounded parent
output collection. It does not expose the application workspace to parser code and does not
silently downgrade to portable mode. Tests probe raw native socket/fork/exec attempts, unrelated
host-file reads, missing isolation and output overproduction. See SECURITY.md for scope and
platform prerequisites. Explicit external subject/verifier programs are a separate trust boundary.

## Accessibility continuation

The required installed-wheel journey now includes automated WCAG 2/2.1 A/AA audits on nine
populated screens. A negative control verifies that an unlabeled button is detected. Incorrectly
named generic regions now have explicit accessible region roles. The release artifact retains
engine details and incomplete checks as a manual-review queue; no manual check is counted as a
pass. Keyboard navigation remains a separate exercised path. Screen-reader review is still open.

Current local evidence: **365 Python tests passed**, all nine requested populated-screen audits
completed without automated WCAG A/AA violations, the deliberate negative control was detected,
and installed-wheel/built-artifact checks passed. [The saved evidence](validation/linux-python312.json)
identifies source bytes and preserves manual review targets. The declared privileged Linux/Python
3.12 CI target also passed the full suite, clean installed-wheel check, browser journey and
automated accessibility gate on 2026-10-23; this does not establish broader platform support.

## Next work

1. Complete manual accessibility review and remaining failure-state coverage; complete dependency/provenance review.
2. Expand platform and browser validation before widening support claims.
3. Take an owner review checkpoint for the exact release artifact and public files.
4. Continue optional laboratory breadth and reviewed jurisdiction packs against their separate
   acceptance criteria. No machine-generated pack should imply legal certification.

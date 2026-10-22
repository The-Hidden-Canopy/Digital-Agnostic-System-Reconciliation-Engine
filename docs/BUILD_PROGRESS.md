# Build progress against the docuseries

Assessment: 22 October 2026. Baseline: `docs/docuseries/`, added in `cf1be07`.
This is a working narrow implementation with substantial advanced modules. It is not the full
laboratory or a reviewed public release. Chapter 13 explicitly allows optional jurisdiction packs
and advanced breadth to follow a usable generic release.

| Build area | Current implementation and evidence | Remaining scope |
|---|---|---|
| Contracts and deterministic dispatch (01–02) | `model.py`, `canonical.py`, `validation.py`, `engine.py`; contract vectors, hash-seed and adversarial tests | Broader platform evidence; additional rule families need their own witness contracts |
| Composition (03) | `composition.py`, `graph.py`; cycle/closure/tag/unit obligations, targeted scope, change-impact paths, independent checker, UI | Transitive-evidence cache keys; wider constraint vocabulary |
| Purpose-relative completeness (04) | `purposes.py`; role binding, generation coherence, lifecycle obligations, UI/export/checker | Conditional applicability and richer coherence rules; user profile editing |
| Stateful experiments and reduction (05, 07) | Independent reference/candidate KV stores, three-way replay, crash enumeration, bounded reducer, fresh-process counterexample verification; external subject adapter | Other proposed synthetic systems (transformation pipeline and seeded scheduler) with their own witnessed defects and reduced reproducers; hardened executable isolation |
| Temporal reasoning (06) | Exact decimal interval reasoning and `interval.relation`; independent re-derivation | Richer temporal UI and additional rule families |
| Admission and document review (08, 17) | PDF/JSON/Markdown/SQL, subprocess limits, coverage, exact spans, append-only review, stale runs; adversarial and workflow suites | Hardened OS sandbox, optional OCR, trusted signatures, arbitrary free-prose claim authoring; directory-package admission is outside the current upload path |
| Campaigns and independent reports (09–10) | Fixed mutation/metamorphic manifests, defective controls, independent totals/witness checking, external verifier adapter | Broader mutation/rule coverage and independently implemented language/runtime checker |
| Generic risk packs (11) | Strict data-only grammar, three-valued applicability, dependency/version checks, UI obligations and proposed remediation | Catalog breadth; reviewed jurisdiction source mappings |
| Packaging and release evidence (12, chapter 13 K) | `tools/check_release.py`: hashed Python build/parser inputs, two matching clean wheel builds, exact content allowlist, isolated offline installs, four-format CLI and optional installed-wheel Chromium journey; CI definition added | CI must actually execute remotely; broader platform matrix; full provenance/license review and owner release review |
| Workbench (16) | Shared backend, default five-step workflow, job cancellation/recovery, findings/spans, advanced composition/purpose/campaign pages, four-format browser journey | Screen-reader/automated accessibility audit; broader failure-state/browser coverage; advanced graph/replay visualizations |
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

## Next work

1. Execute the new CI on the declared Linux/Python 3.12 target and retain its exact evidence.
2. Audit accessibility and remaining failure states; complete dependency/provenance review.
3. Expand platform and browser validation before widening support claims.
4. Take an owner review checkpoint for the exact release artifact and public files.
5. Continue optional laboratory breadth and reviewed jurisdiction packs against their separate
   acceptance criteria. No machine-generated pack should imply legal certification.

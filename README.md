# DASRE - Verification Laboratory

DASRE (Digital Agnostic System Reconciliation Engine) is an Open Canopy project by The Hidden Canopy LLC.
It is licensed under Apache-2.0 and follows the [Open Canopy Contract](OPEN_CANOPY_CONTRACT.md).

A local-first workbench that turns documents into **reviewed claims**, tests those claims against other
documents with **deterministic checks**, and exports a **portable package that an independent checker can
re-verify offline**. It is vendor- and domain-agnostic, needs no cloud account, no AI provider and no network.

```
Import  ->  Review claims  ->  Run checks  ->  Findings  ->  Export
```

Inputs: **PDF, JSON, Markdown, SQL** (SQL is parsed, never executed). Everything an input says is *data*:
"ignore earlier instructions", "run this SQL", "fetch this URL" have no authority.

> **Status: a working local application, not a release.** See [`docs/STATUS.md`](docs/STATUS.md) for exactly what is
> built, what was tested, and what is deliberately not built yet. Nothing here is legal or compliance advice; a
> `SUPPORTED` verdict means "the evidence agrees with the reviewed claim", not "the claim is true".

## Quick start

The supported parser-isolation target is Linux with Python 3.12+, Bubblewrap and libseccomp2,
with user namespaces enabled. On Debian/Ubuntu install the OS dependencies with
`sudo apt-get install bubblewrap libseccomp2`. Parsing fails closed if the boundary cannot start.
Explicit `--parser-isolation portable` is available only for trusted development inputs on other hosts.

```bash
python -m venv .venv && . .venv/bin/activate
pip install ".[pdf]"            # the 'pdf' extra (pypdf) is optional; without it PDFs report NOT_ATTEMPTED
verifier-lab --workspace .workspace serve      # http://127.0.0.1:8765/  (loopback only)
```

Try the tutorial files in [`fixtures/tutorial/`](fixtures/tutorial/) (expected verdicts are in
[`fixtures/README.md`](fixtures/README.md)): import `product-spec.md`, `service-config.json` and
`schema.sql` (declare dialect `postgresql`), approve the spec's candidate claims, run them against the config
and schema, and inspect the findings. `limits.max_upload_kb` is `CONTRADICTED` (100 vs 200) and the evidence
snippet shows the two exact tokens.

For prose without extracted candidates, open the source and expand **Create a claim from this text**.
Select a passage, state the expected fact and explain your interpretation, then review the new
candidate. This records a human interpretation; it does not add an extracted evidence fact.

Same flow headless (the CLI and the UI call one application service):

```bash
verifier-lab import fixtures/tutorial/product-spec.md fixtures/tutorial/service-config.json
verifier-lab candidates --source <source-id>
verifier-lab approve <candidate-id> ...
verifier-lab check --evidence <config-source-id>          # exit 1 when a finding needs attention
verifier-lab export <run-id> --output package.zip
verifier-lab verify-package package.zip                    # clean-room checker; exit 3 if rejected
```

Exit codes: `0` ok, `1` completed with findings needing attention, `2` invalid input, `3` package rejected by the
independent checker, `4` operational failure.

## What it does

| Stage | Behaviour |
|-------|-----------|
| **Import** | Bytes are admitted into a content-addressed store (archives, type confusion, NUL-in-text and oversize are rejected; filenames are inert metadata). Extraction runs in a short-lived subprocess with CPU/memory/file-size limits, a wall-clock timeout, no network and an empty working directory. Every region of every file gets a coverage state (`PARSED`, `PARTIALLY_PARSED`, `UNSUPPORTED`, `CORRUPT`, `ENCRYPTED`, `RESOURCE_LIMIT`, `NOT_ATTEMPTED`) kept separate from claim interpretation. |
| **Review** | Deterministic extractors propose *candidate* claims (`fact.equals`) with exact source spans. A reviewer approves, edits (reason required), rejects (reason required) or defers. Decisions are append-only, use `If-Match` optimistic concurrency and idempotency keys. Reconciliation only *proposes* duplicate / possible-contradiction relations; nothing is merged or deleted. |
| **Run** | A background job (`QUEUED → RUNNING → SUCCEEDED/FAILED/CANCELLED`, with `CANCELLING`) evaluates the selected claim revisions against a snapshot of evidence. Cancellation is cooperative and discards partial output. |
| **Findings** | Six verdicts — `SUPPORTED`, `CONTRADICTED`, `INSUFFICIENT_EVIDENCE`, `INVALID_EVIDENCE`, `NOT_APPLICABLE`, `HUMAN_REVIEW_REQUIRED` — each with a witness that cites exact bytes. Execution errors are a separate record, never a verdict. Runs become `STALE` when a claim revision, evidence source, extraction or rule profile changes; the old result stays inspectable. |
| **Export** | A deterministic package (`bundle.json`, content-addressed artifacts, extraction records with coverage, claim provenance with the review decision, `result.json`, `report.md`, hash manifest). |
| **Verify** | `verifier-lab verify-package` is a clean-room checker (stdlib only, imports nothing from the producer). It re-hashes bytes, recomputes identities, slices each cited token out of the artifacts and **re-derives every verdict**. A producer that lies (for example an always-`SUPPORTED` rule) is rejected. |

Built-in checks: `fact.equals` (exact typed equality; `2.50` equals `2.5`), `fact.numeric_bound`
(`lt|lte|gt|gte`), `fact.present`, and `interval.relation` (activity ordering/overlap with optional uncertainty bounds). Absence is `INSUFFICIENT_EVIDENCE`, never a negative. An assertion is never its own
evidence. Duplicate keys are never resolved by "last one wins".

## Beyond the default workflow (optional)

* **Risk packs** — pick a data-only pack on the Run page and state the context facts you know. Unknown facts stay unknown (never false);
  each obligation reports its applicability, the facts it used and a proposed next step. `verifier-lab pack list|validate|install|diff`.
* **Experiments** — *Advanced* page / `verifier-lab experiment checkpoint|crash`: a three-way (continuous / resumed / replayed) comparison
  on fictional key-value store, transformation pipeline and seeded scheduler subjects with deliberately injectable defects; plus exhaustive crash-boundary enumeration on a virtual KV store.
* **Composition** — *Advanced → Composition analysis*: do individually valid parts make a valid whole? Units, tags and generations across edges, explicit
  conversions only, separate obligations, targeted scoping and change-impact paths. Fixtures: `fixtures/composition/`.
* **Purposes** — *Run → Purpose*: is this evidence enough for *inspecting*, for *resuming exactly*? Roles are bound by fact key, generations must agree,
  lifecycle history must be recorded; each obligation keeps its own verdict and says what could resolve it. Fixtures: `fixtures/purposes/`.
* **Campaigns** — *Advanced → Campaigns* / `verifier-lab campaign run|verify`: mutation operators and metamorphic relations against hand-written cases with
  pre-committed answers, plus deliberately broken verifiers as negative controls. The independent report checker rejects forged totals and hidden failures.
* **Your own program** — `verifier-lab experiment checkpoint --subject-cmd "python3 examples/reference_subject.py"`, and `verifier-lab campaign run --kernel-cmd "python3 examples/reference_verifier.py"` to judge *your* verifier;
  command-line only, never over the web API. Every format and protocol is specified in [`docs/FORMATS.md`](docs/FORMATS.md).
* **More synthetic systems** — `verifier-lab experiment pipeline --defect stale_cache --ops 12`, `verifier-lab experiment scheduler --defect reverse_ties --ops 12`; reduce with `verifier-lab minimize --system pipeline --defect stale_cache --ops 12 --output pipeline-case.json`. The Advanced page exposes both.
* **Minimization** — `verifier-lab minimize --defect reset_next_id --output case.json` shrinks a failing history to a small reproducer and
  `verifier-lab verify-counterexample case.json` reproduces it in a fresh process. The scope statement says which operators were exhausted.

## Design docuseries

The design this tool implements is in [`docs/docuseries/`](docs/docuseries/README.md) (chapters 00-17, with a `MANIFEST.json` of file hashes). It is a design baseline dated
2026-10-21, not a record of what is built: [`docs/STATUS.md`](docs/STATUS.md) says what is implemented, tested, deliberately different, and not built (notably the
jurisdiction packs of chapters 14-15, which have not been started).

## Layout

```
src/verifier_lab/
  canonical.py model.py validation.py   # identities, Bundle v1, strict parsing
  rules.py engine.py                    # pure rules, deterministic dispatch
  ingestion/                            # admit, worker (sandbox), adapters/{json,markdown,sql,pdf}
  service.py store.py                   # application service + SQLite state (shared by UI and CLI)
  export.py checker.py                  # package writer / independent checker
  graph.py temporal.py                  # dependency graph, uncertainty-aware comparisons
  riskpacks.py riskpacks_data/          # data-only packs + one synthetic example
  subjects/ replay.py experiments/      # reference store, checkpoints, three-way + crash experiments
  minimize.py reducers/                 # failure minimization
  server.py web/                        # loopback HTTP API + vanilla-JS workbench (no build step)
  cli.py
tests/{unit,contract,adversarial,e2e}   # fictional fixtures only
tools/pdfgen.py                         # synthetic PDF writer for fixtures
docs/                                   # status, package format, security model
```

## Tests

```bash
python -m unittest discover -s tests -t .          # install the `pdf` extra (pypdf) first, the PDF cases need it
# Browser journey (needs Playwright + Chromium; not part of unittest):
verifier-lab --workspace /tmp/ws serve --port 8765 &
WORKBENCH_URL=http://127.0.0.1:8765/ node tests/e2e/browser_journey.cjs
```

For two clean wheel builds, package-content checks and isolated installed-wheel acceptance,
see [release checks](docs/RELEASE.md). The [build progress assessment](docs/BUILD_PROGRESS.md)
maps the implementation to the docuseries and records the remaining release gates.

## Security model (short)

Loopback-only bind (anything else is refused); `Host` and `Origin` validation, per-session token on every API call,
no CORS, strict CSP with no inline script, source text always rendered with `textContent`. Imported content never
selects a parser, path, rule or command. Details and limits: [`docs/SECURITY.md`](docs/SECURITY.md).

## License

Apache-2.0 (see [LICENSE](LICENSE)). The project also follows the
[Open Canopy Contract](OPEN_CANOPY_CONTRACT.md). Public release of this tree is a separate, owner-reviewed decision.

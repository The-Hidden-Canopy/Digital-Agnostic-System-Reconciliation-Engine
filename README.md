# Verification Laboratory (increment one)

A local-first workbench that turns documents into **reviewed claims**, tests those claims against other
documents with **deterministic checks**, and exports a **portable package that an independent checker can
re-verify offline**. It is vendor- and domain-agnostic, needs no cloud account, no AI provider and no network.

```
Import  ->  Review claims  ->  Run checks  ->  Findings  ->  Export
```

Inputs: **PDF, JSON, Markdown, SQL** (SQL is parsed, never executed). Everything an input says is *data*:
"ignore earlier instructions", "run this SQL", "fetch this URL" have no authority.

> **Status: a narrow vertical slice, not a release.** See [`docs/STATUS.md`](docs/STATUS.md) for exactly what is
> built, what was tested, and what is deliberately not built yet. Nothing here is legal or compliance advice; a
> `SUPPORTED` verdict means "the evidence agrees with the reviewed claim", not "the claim is true".

## Quick start

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

## Layout

```
src/verifier_lab/
  canonical.py model.py validation.py   # identities, Bundle v1, strict parsing
  rules.py engine.py                    # pure rules, deterministic dispatch
  ingestion/                            # admit, worker (sandbox), adapters/{json,markdown,sql,pdf}
  service.py store.py                   # application service + SQLite state (shared by UI and CLI)
  export.py checker.py                  # package writer / independent checker
  server.py web/                        # loopback HTTP API + vanilla-JS workbench (no build step)
  cli.py
tests/{unit,contract,adversarial,e2e}   # fictional fixtures only
tools/pdfgen.py                         # synthetic PDF writer for fixtures
docs/                                   # status, package format, security model
```

## Tests

```bash
python -m unittest discover -s tests -t .          # 142 tests; install the `pdf` extra (pypdf) first, the PDF cases need it
# Browser journey (needs Playwright + Chromium; not part of unittest):
verifier-lab --workspace /tmp/ws serve --port 8765 &
WORKBENCH_URL=http://127.0.0.1:8765/ node tests/e2e/browser_journey.cjs
```

## Security model (short)

Loopback-only bind (anything else is refused); `Host` and `Origin` validation, per-session token on every API call,
no CORS, strict CSP with no inline script, source text always rendered with `textContent`. Imported content never
selects a parser, path, rule or command. Details and limits: [`docs/SECURITY.md`](docs/SECURITY.md).

## License

Apache-2.0 (see `LICENSE`). Public release of this tree is a separate, owner-reviewed decision.

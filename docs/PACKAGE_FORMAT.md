# Package format v1 (so another implementation can verify without this code)

A package is a directory or a ZIP with exactly these entries (no others):

```
MANIFEST.json        canonical JSON: {schema, bundle_id, files:[{path, sha256, size}], observation_files:["run_metadata.json"]}
bundle.json          Bundle v1 (schema_version "1", bundle_id, artifacts, claims, activities, assumptions, profile_refs)
result.json          findings, execution errors, claim index, completion, plan, limitations
run_metadata.json    observation metadata (timings, timestamps); never part of semantic identity; may contain floats
report.md            human-readable rendering of result.json
artifacts/<hex>      content-addressed bytes: originals, text renditions, extraction records, claim provenance
```

**Canonical JSON:** UTF-8, object keys sorted by code point, compact separators, no floats/NaN, no duplicate keys.
**Identities:** `<kind>:sha256:<hex>` where hex = SHA-256 over `kind` + `0x00` + canonical bytes of the record body without
its own `id` (for the bundle, without `bundle_id`). Artifact `digest` is `sha256:<hex>` of the raw file bytes.
Top-level collections are sorted by id; set-like reference lists are sorted.

**Artifact media types the checker understands**
* `application/vnd.verifier-lab.extraction-record+json` — manifest, coverage regions, `facts[]` (key, typed value,
  grammar, locator byte range into the *rendition*, `token_digest`), `blocked_keys[]`, diagnostics.
* `application/vnd.verifier-lab.claim-provenance+json` — origin span, review decision (action, reason, edited), original
  candidate predicate.

**Value grammars** (token bytes → typed value): `json-literal`, `plain-scalar`, `sql-literal`, `sql-flag`.
Typed values are `{"kind": string|integer|decimal|boolean|null, "value": ...}`; numerics are exact decimal strings and
compare numerically (`2.50 == 2.5`); differing kinds are *incomparable* (needs human review), not "not equal".

**Checker obligations** (what `verifier-lab verify-package` does): hash every listed file and reject unlisted ones;
recompute every record id; verify artifact digests/paths; verify claim provenance bytes; for each finding re-derive the
verdict and reason from verified token bytes, compare the witness observations, and reject anything not re-derivable.
Findings from rule families the checker lacks are reported under `unsupported`, never silently accepted.

**Known checker limits** (also printed in its output): facts the extractor never recorded cannot be detected; fact keys
are checked against cited context only; PDF text derivation is not reproduced; no signatures or trust anchors.

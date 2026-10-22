# Composition fixtures (fictional)

Each file has a top-level `pipeline` object. Every activity is *locally* fine; the question is whether the whole composes.

| File | Expected `composition.valid` | Why |
|---|---|---|
| `good.json` | SUPPORTED (conditional on a declared external assumption and unverified producer tags) | requirements and generations agree |
| `units-mismatch.json` | CONTRADICTED `tag_mismatch` | one activity emits milliseconds, the next requires seconds; no conversion activity between them |
| `units-converted.json` | SUPPORTED | an explicit conversion activity with the registered exact factor (0.001) sits between them |
| `units-bad-conversion.json` | CONTRADICTED `conversion_factor_wrong` | the declared factor 0.01 disagrees with the registered exact factor |
| `offsets-mismatch.json` | CONTRADICTED `tag_mismatch` | the consumer needs `original_bytes` offsets, the producer yields `normalized` ones |
| `missing-input.json` | INSUFFICIENT_EVIDENCE `undeclared_input` | acyclic, but an input has neither a producer nor a declared external source |
| `cycle.json` | CONTRADICTED `cycle` | `a -> b -> a`; version one graphs are acyclic and no cycle is silently cut |
| `generation-mismatch.json` | CONTRADICTED `generations_differ` | two artifacts that must share a generation come from g1 and g2 |
| `scoped.json` | INSUFFICIENT_EVIDENCE for the whole graph; SUPPORTED with `target: "report"` | the broken `side` branch is not an ancestor of `report`, so it neither blocks nor is verified |

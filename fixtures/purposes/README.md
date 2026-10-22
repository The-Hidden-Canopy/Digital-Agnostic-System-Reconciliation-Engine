# Purpose fixtures (fictional)

Bindings used by the tests and the tutorial: `entries_count -> checkpoint.entries_count`, `allocator_next -> checkpoint.allocator_next`,
`random_cursor -> checkpoint.random_cursor`, `operation_cursor -> checkpoint.operation_cursor`, `semantics_version -> checkpoint.semantics_version`.

| Evidence supplied | inspect-checkpoint | resume-exactly | Why |
|---|---|---|---|
| `map.json` | SUPPORTED | INSUFFICIENT_EVIDENCE | the map is enough to look at, but counters, coherence and lifecycle are missing |
| `map.json` + `counters.json` | SUPPORTED | INSUFFICIENT_EVIDENCE | roles and coherence hold (generation g7) but no lifecycle records exist |
| + `history.json` | SUPPORTED | SUPPORTED | every obligation is met by evidence |
| `map.json` + `counters-g6.json` + `history.json` | SUPPORTED | CONTRADICTED | both files are individually valid but come from different generations (g7 vs g6) |
| `map.json` + `counters-bad-type.json` | SUPPORTED | INVALID_EVIDENCE | `allocator_next` is the string "five": a supplied record of the wrong type is invalid, not missing |
| `history-invalidated.json` (instead of `history.json`) | SUPPORTED | CONTRADICTED | an invalidation follows the success on the same clock |
| `history-other-clock.json` | SUPPORTED | INSUFFICIENT_EVIDENCE | the invalidation is on another clock: ordering is ambiguous, both records are kept |
| `history-no-persist.json` | SUPPORTED | INSUFFICIENT_EVIDENCE | `recoverable` is claimed but its prerequisite `persisted` was never recorded |

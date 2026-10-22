# Security model and known limits

**Assets:** private documents on the user's machine. **Attackers considered:** hostile document content; hostile web
pages in the same browser (CSRF, DNS rebinding); tampered packages.

| Threat | Control | Test |
|---|---|---|
| Instructions inside documents | Content is data only; nothing selects code, paths, rules or fetches | `test_instruction_text_is_inert_data_in_every_format` |
| Archive / type confusion / binary | Magic-byte checks, declared-vs-detected comparison, NUL rejection, size cap | `Admission` tests |
| Path tricks via filenames | Filenames are display metadata only; storage is by SHA-256 | `test_filenames_are_inert_metadata` |
| Parser DoS (nesting, tokens, bombs, memory) | Adapter limits + subprocess rlimits + wall clock; failures are parse diagnostics, never verdicts | hostile-document tests |
| Network/exfiltration from parsers | Socket guard in the worker; empty env/cwd; no adapter has network code | `test_extraction_worker_blocks_network_and_file_writes` |
| Cross-site requests / rebinding | Loopback bind only, `Host`/`Origin` checks, session token header, no CORS, JSON-only mutations | `Security` tests |
| XSS from source text | `textContent` rendering only, CSP without `unsafe-inline`, no `innerHTML` in the UI (asserted) | `test_ui_assets_...` |
| Package tampering | Hash manifest + identity recomputation + verdict re-derivation | `Tamper` tests |

| Malicious or sloppy risk packs | Data-only grammar; unknown/executable-looking fields rejected; rules resolve only to the installed catalog; digest pinning; conflicting content under one identity rejected | `test_riskpacks`, `test_server::PacksAndExperiments` |
| Resource abuse via experiment endpoints | Bounded operations/oracle calls; experiments touch only the in-memory fictional store | `PacksAndExperiments` |

| Running a user-supplied subject | Only from an explicit CLI flag; HTTP API rejects command fields; packages never carry runnable commands (identity only); empty cwd, minimal env, rlimits, timeouts | `test_external_subject`, `NoCommandsOverHttp` |

**Not claimed:** a hardened sandbox (no seccomp/namespaces; the guard is defence in depth, not a boundary against
native-code exploits in a parser dependency such as pypdf); protection if another local process can read the workspace
directory or the loopback port while it runs (the token is embedded in the served page); multi-user or remote
deployment (a separate release gate); authenticity of authorship (no signatures yet).

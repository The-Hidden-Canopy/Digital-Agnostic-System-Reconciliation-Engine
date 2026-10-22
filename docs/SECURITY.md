# Security model and known limits

**Assets:** private documents on the user's machine. **Attackers considered:** hostile document content; hostile web
pages in the same browser (CSRF, DNS rebinding); tampered packages.

| Threat | Control | Test |
|---|---|---|
| Instructions inside documents | Content is data only; nothing selects code, paths, rules or fetches | `test_instruction_text_is_inert_data_in_every_format` |
| Archive / type confusion / binary | Magic-byte checks, declared-vs-detected comparison, NUL rejection, size cap | `Admission` tests |
| Path tricks via filenames | Filenames are display metadata only; storage is by SHA-256 | `test_filenames_are_inert_metadata` |
| Parser DoS (nesting, tokens, bombs, memory) | Adapter limits + subprocess rlimits + wall clock; failures are parse diagnostics, never verdicts | hostile-document tests |
| Network/exfiltration from parsers | Private network namespace plus native socket syscall denial; empty environment; Python socket guard | `test_os_isolation`, `test_extraction_worker_blocks_network_and_file_writes` |
| Cross-site requests / rebinding | Loopback bind only, `Host`/`Origin` checks, session token header, no CORS, JSON-only mutations | `Security` tests |
| XSS from source text | `textContent` rendering only, CSP without `unsafe-inline`, no `innerHTML` in the UI (asserted) | `test_ui_assets_...` |
| Package tampering | Hash manifest + identity recomputation + verdict re-derivation | `Tamper` tests |

| Malicious or sloppy risk packs | Data-only grammar; unknown/executable-looking fields rejected; rules resolve only to the installed catalog; digest pinning; conflicting content under one identity rejected | `test_riskpacks`, `test_server::PacksAndExperiments` |
| Resource abuse via experiment endpoints | Bounded operations/oracle calls; experiments touch only the in-memory fictional store | `PacksAndExperiments` |

| Running a user-supplied subject | Only from an explicit CLI flag; HTTP API rejects command fields; packages never carry runnable commands (identity only); empty cwd, minimal env, rlimits, timeouts | `test_external_subject`, `NoCommandsOverHttp` |

## Required parser boundary (Linux)

The default `required` mode starts Bubblewrap with private user, PID, mount, IPC, UTS and network
namespaces. Only the Python runtime/virtualenv, application package code and system libraries are
mounted read-only. The host workspace, source documents, credentials and host temporary directory
are not mounted; source bytes enter through stdin. The worker has an empty environment, a private
working directory and a bounded private temporary filesystem. POSIX CPU/address-space/file-size
limits and a parent-enforced wall deadline still apply.

After bootstrap, libseccomp denies native sockets, new processes, executable replacement,
namespace/privilege operations and process inspection/signaling. These rules apply to raw native
syscalls as well as Python APIs. Parent stdout/stderr collection has enforced byte limits and
terminates an overproducing worker before unbounded accumulation. Tests probe actual host-file
visibility, native socket/fork/exec denial and namespace separation.

Linux, Bubblewrap, libseccomp2 and working unprivileged user namespaces are prerequisites. Failure
to establish them is `isolation_unavailable`; parsing does not silently fall back. In cloud command
sandboxes that prohibit nested UID mapping, run parser integration on the supported host execution
surface. A trusted developer may explicitly select `--parser-isolation portable` for a host that
cannot establish this boundary. That mode retains resource/time limits and the Python socket guard,
but is only for trusted development inputs and has no hostile-native-code isolation claim. The
configured mode is included in extraction limits/provenance and capabilities.

**Not claimed:** an independent sandbox/security audit; isolation of explicitly supplied external
subject/verifier programs (their documented resource containment remains separate); protection if
another local process can read the workspace or loopback port (the token is embedded in the served
page); multi-user/remote deployment; authenticity of authorship without trusted signatures.

# exec

Shell execution and supervised background processes inside the workspace.

This package is a thin adapter: `create_plugin` forwards to the shared `voidcode.capabilities.exec` factory.

## Surface

- Tool `shell_exec` (`command`, optional `description`, `timeout` 1–600s, default 120s). Effects `EXECUTE` + `SPAWN`. Returns exit/timeout/cancel state, bounded model-visible stdout/stderr pages, and typed references to complete captured UTF-8 text streams (including empty streams).
- Tool `background_process` with `op` `start` | `ps` | `logs` | `send` | `stop`. Effects `EXECUTE` + `SPAWN`. `start` takes `command`, optional `description`, and `ready_pattern`/`ready_timeout` (1–30s); `logs` pages lines (`stream`, `offset`, `limit` ≤ 500); `send` writes stdin; `stop` terminates the whole process group.

## Resource URIs

- `background_process start` returns `resource_uri` as `proc://<opaque-id>`. `read proc://` lists only process records visible to the caller's session; `read proc://<id>` returns current status and a stdout byte page.
- `read proc://<id>/stderr` selects stderr. This is a VoidCode-specific adaptation to the existing two-stream process API, not a copied OMP route; the root URI is stdout and there is no `/stdout` alias.
- The inventory accepts `read` line `offset`/`limit`; process output accepts only `byte_offset`/`byte_limit`. Public `read` admits byte limits from 4 through `MAX_RESOURCE_PAGE_BYTES` before dispatch. Accepted limits reach Core unchanged, where `ProcessOutput` enforces its separate 256 KiB page bound; either validation rejects visibly rather than silently clipping.
- Process byte offsets address captured raw bytes. Valid UTF-8 is returned without replacement or newline normalization; pages that are not valid UTF-8 are shown as exact hex. `ProcessOutput.eof` means the page reached the current captured end, not that the process exited. `running` is separate; at a running captured end there is no immediate next page, and `current_end_byte_offset` is the position to reread after output advances.
- Resource bodies contain no command, PID, environment, or private log path. Reads use only the current Core's owner- and session-scoped process status/output APIs; they never bind, signal, or control a process. If a record is no longer supervised, current liveness is unknown and logs are unavailable; only records with persisted session attribution are shown.


## Configuration

Takes no plugin config; `create_plugin` ignores it. See [configuration](../../../../docs/configuration.md) for global config.

## Behavior and limits

- Both tools run under the core's process lease, which owns process groups, per-stream capture bounds, and timeout/cancellation. The tools own the contract around it: timeout validation, preview/artifact split, and failure shapes.

- The process service currently captures bounded bytes in memory, then decodes UTF-8 with replacement and normalizes line endings before shell artifacts are stored. These references recover captured text, not byte-identical raw subprocess streams; see the SDK raw/streaming capture gap.
- Shell and web summaries use `EmptyOutput` plus typed text references. Core pages only explicitly out-of-line content into the model budget; `TextOutput.text` stays the producer-selected page. Structured bodies are not duplicated beside selected text and remain recoverable through the canonical result URI when omitted.
- Persisted background-process records are history. The existing `background_process` control tool reports stale identities as externally managed when appropriate, but `proc://` never infers liveness or restores control after a Core restart. Legacy records without session attribution are not listed. Deactivation runs `prepare_exec_deactivation`.

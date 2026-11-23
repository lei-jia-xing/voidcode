# exec

Shell execution and supervised background processes inside the workspace.

This package is a thin adapter: `create_plugin` forwards to the shared `voidcode.capabilities.exec` factory.

## Surface

- Tool `shell_exec` (`command`, optional `description`, `timeout` 1–600s, default 120s). Effects `EXECUTE`. Returns exit/timeout/cancel state, bounded model-visible stdout/stderr pages, and typed references to complete captured UTF-8 text streams (including empty streams).
- Tool `background_process` with `op` `start` | `ps` | `logs` | `send` | `stop`. Effects `EXECUTE`. `start` takes `command`, optional `description`, and `ready_pattern`/`ready_timeout` (1–30s); `logs` pages lines (`stream`, `offset`, `limit` ≤ 500); `send` writes stdin; `stop` terminates the whole process group. `SPAWN` is reserved for delegation, not OS-process effects.
- `ps` renders every visible process in its model text as `id (proc://id): state`, plus exit code, description, and command. Each row is character-capped and the whole text is render-budget bounded; omitted rows are disclosed and the typed body retains them. Rows and persisted records are filtered to the caller's session; another session's id is indistinguishable from an unknown id. Private spool paths stay out of model text.
- `start` checks cancellation after the spawn, after persisting the identity, and after the readiness wait. A cancelled start stops the process group through the core service; when the stop is confirmed the persisted record is marked exited. It never returns a success note for a cancelled readiness wait.
- `send` accepts at most 64 KiB per call (schema `maxLength` in characters, plus an exact UTF-8 byte check). The core write is non-blocking and bounded by a deadline and the lease's cancel event, so it cannot park a writer thread; a partial write returns the exact accepted byte count as a failure.
- `logs` puts the real `next_offset` / `next_offsets` values in model text and enforces a per-line character cap plus a total rendered-byte budget, so one 500-line page cannot overflow the model projection. `truncated` means the page was clipped, the scan cap was hit, or a next page exists; it is never set by `offset > 0` alone. When the 8 MiB scan cap is hit, guidance points at `read(proc://<id>[/stderr], byte_offset=...)` and never at a spool path.

## Resource URIs

- `background_process start` returns `resource_uri` as `proc://<opaque-id>`. `read proc://` lists only process records visible to the caller's session; `read proc://<id>` returns current status and a stdout byte page.
- `read proc://<id>/stderr` selects stderr. This is a VoidCode-specific adaptation to the existing two-stream process API, not a copied OMP route; the root URI is stdout and there is no `/stdout` alias.
- The inventory accepts `read` line `offset`/`limit`; process output accepts only `byte_offset`/`byte_limit`. Public `read` admits byte limits from 4 through `MAX_RESOURCE_PAGE_BYTES` before dispatch. An omitted byte limit uses `MAX_RESOURCE_PAGE_BYTES` (leaving room for the text envelope), not the host's larger internal page bound; explicit limits still reach Core unchanged where `ProcessOutput` enforces its separate 256 KiB page bound. Either validation rejects visibly rather than silently clipping.
- Process byte offsets address captured raw bytes. Valid UTF-8 is returned without replacement or newline normalization; pages that are not valid UTF-8 are shown as exact hex. `ProcessOutput.eof` means the page reached the current captured end, not that the process exited. `running` is separate; at a running captured end there is no immediate next page, and `current_end_byte_offset` is the position to reread after output advances.
- Resource bodies contain no command, PID, environment, or private log path. Reads use only the current Core's owner- and session-scoped process status/output APIs; they never bind, signal, or control a process. If a record is no longer supervised, current liveness is unknown and logs are unavailable; only records with persisted session attribution are shown.


## Configuration

Takes no plugin config; `create_plugin` ignores it. See [configuration](../../../../docs/configuration.md) for global config.

## Behavior and limits

- Both tools run under the core's process lease, which owns process groups, per-stream capture bounds, and timeout/cancellation. The tools own the contract around it: timeout validation, preview/artifact split, and failure shapes.

- The process service currently captures bounded bytes in memory, then decodes UTF-8 with replacement and normalizes line endings before shell artifacts are stored. These references recover captured text, not byte-identical raw subprocess streams; see the SDK raw/streaming capture gap.
- Shell and web summaries use `EmptyOutput` plus typed text references. Core pages only explicitly out-of-line content into the model budget; `TextOutput.text` stays the producer-selected page. Structured bodies are not duplicated beside selected text and remain recoverable through the canonical result URI when omitted.
- Persisted background-process records are history. The existing `background_process` control tool reports stale identities as externally managed when appropriate, but `proc://` never infers liveness or restores control after a Core restart. Legacy records without session attribution are not listed. Deactivation runs `prepare_exec_deactivation`.

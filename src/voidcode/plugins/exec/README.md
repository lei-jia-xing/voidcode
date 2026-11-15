# exec

Shell execution and supervised background processes inside the workspace.

This package is a thin adapter: `create_plugin` forwards to the shared `voidcode.capabilities.exec` factory.

## Surface

- Tool `shell_exec` (`command`, optional `description`, `timeout` 1–600s, default 120s). Effects `EXECUTE` + `SPAWN`. Returns exit/timeout/cancel state, bounded model-visible stdout/stderr pages, and typed references to complete captured UTF-8 text streams (including empty streams).
- Tool `background_process` with `op` `start` | `ps` | `logs` | `send` | `stop`. Effects `EXECUTE` + `SPAWN`. `start` takes `command`, optional `description`, and `ready_pattern`/`ready_timeout` (1–30s); `logs` pages lines (`stream`, `offset`, `limit` ≤ 500); `send` writes stdin; `stop` terminates the whole process group.

## Configuration

Takes no plugin config; `create_plugin` ignores it. See [configuration](../../../../docs/configuration.md) for global config.

## Behavior and limits

- Both tools run under the core's process lease, which owns process groups, per-stream capture bounds, and timeout/cancellation. The tools own the contract around it: timeout validation, preview/artifact split, and failure shapes.

- The process service currently captures bounded bytes in memory, then decodes UTF-8 with replacement and normalizes line endings before shell artifacts are stored. These references recover captured text, not byte-identical raw subprocess streams; see the SDK raw/streaming capture gap.
- Shell and web summaries use `EmptyOutput` plus typed text references. Core pages only explicitly out-of-line content into the model budget; `TextOutput.text` stays the producer-selected page. Structured bodies are not duplicated beside selected text and remain recoverable through the canonical result URI when omitted.
- Persisted background-process records are history: after a host restart a process is externally managed — logs, stdin, and its process group are unavailable — and every `start` mints a fresh identity. Deactivation runs `prepare_exec_deactivation`.

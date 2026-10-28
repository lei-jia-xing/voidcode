# exec

Shell execution and supervised background processes inside the workspace.

This package is a thin adapter: `create_plugin` forwards to the shared `voidcode.capabilities.exec` factory.

## Surface

- Tool `shell_exec` (`command`, optional `description`, `timeout` 1–600s, default 120s). Effects `EXECUTE` + `SPAWN`. Returns exit code, bounded stdout/stderr previews, timeout/cancel flags, and artifacts holding the full captured streams.
- Tool `background_process` with `op` `start` | `ps` | `logs` | `send` | `stop`. Effects `EXECUTE` + `SPAWN`. `start` takes `command`, optional `description`, and `ready_pattern`/`ready_timeout` (1–30s); `logs` pages lines (`stream`, `offset`, `limit` ≤ 500); `send` writes stdin; `stop` terminates the whole process group.

## Configuration

Takes no plugin config; `create_plugin` ignores it. See [configuration](../../../../docs/configuration.md) for global config.

## Behavior and limits

- Both tools run under the core's process lease, which owns process groups, per-stream capture bounds, and timeout/cancellation. The tools own the contract around it: timeout validation, preview/artifact split, and failure shapes.
- Persisted background-process records are history: after a host restart a process is externally managed — logs, stdin, and its process group are unavailable — and every `start` mints a fresh identity. Deactivation runs `prepare_exec_deactivation`.

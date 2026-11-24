# user_shell

Run a one-shot, non-interactive shell command on the core workspace through the human `!` escape.

## Surface

- Command id `shell` with required `command` and optional `timeout` (integer 1–600s, default 600s), callable by RPC/CLI. The only client entry surface is the `!` prefix; it is not exposed as a slash command. Effect `EXECUTE`, `busy_gate=False`.
- A command that exits 0 and stores its artifacts returns `status="ok"`. A nonzero exit, timeout, cancellation, or artifact failure returns `status="error"` with a stable `data.error` code and guidance, so CLI/RPC exit status is honest.
- The human-facing output is a bounded preview (16 KiB) plus artifact URIs when the preview is clipped. The full captured `stdout` and `stderr` are stored separately as artifacts at the exec capability's 128 MiB per-stream capture ceiling; `capture_truncated` says the capture itself was clipped, while artifact presence is not a truncation flag. Empty streams are stored too.
- An executed command stages a compact `shell_executed` event with exit/status/duration/artifact references and failure metadata. It does not stage raw streams, a record string, or the preview; the core's `command_completed` receipt carries the bounded preview and references once.

## Policy

Direct human invocation is the consent boundary: commands have no approval-mode gate, no pre-tool hook gate, and no tool-allowlist check. The `EXECUTE` effect is still refused under `read_only` before any process starts.

`user_shell` and `exec` are independent surfaces. Disabling `exec` removes the model-facing `shell_exec`/background tools but does **not** disable the human `!` escape; disable `user_shell` as well to remove shell execution.

A running command holds a core dispatch slot, so capability activation/reload is refused while it is in flight. The timeout bounds that window; use ESC or `/cancel` in the TUI to cancel an idle-session command too.

## Execution and limits

- Executes via the invocation's process service in the workspace. On POSIX it runs `/bin/sh -c <command>`; on Windows `cmd.exe /c <command>`. There is no login-shell/profile execution.
- Clients strip exactly one leading `!` before dispatch; the remaining command text is passed verbatim (`!!foo` runs `!foo`). An empty command is rejected. Remote clients run the command on the core host, not the client machine.
- No TTY and no streaming: stdin is closed, output is delivered on completion (bounded preview plus artifacts).
- Timeout is configurable from 1 to 600 seconds and defaults to 600. Cancellation preserves only the bounded partial preview; full artifacts are not published for a cancelled command.
- Environment: unconfined runs inherit the core process environment. With the optional `sandbox` section, the sandbox receives its filtered baseline and the command is confined like other registry children.
- Error codes in `data.error`: `empty_command`, `invalid_timeout`, `workspace_unavailable`, `start_failed`, `capture_failed`, `sandbox_failed`, `process_failed`, `artifact_failed`, `event_too_large`, `nonzero_exit`, `timed_out`, `cancelled`. Start/capture/sandbox/process failures state that the command may have run.

## Configuration

Takes no plugin config; `create_plugin` ignores it. See [configuration](../../../../docs/configuration.md) for global config and policy/sandbox semantics.

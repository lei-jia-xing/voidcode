# user_shell

Run a shell command locally through the governed `!` escape.

## Surface

- Command `/shell` with required `command` (non-empty string), exposed to clients as the `!` prefix. Effect `EXECUTE`, `busy_gate=False`.
- Returns combined stdout+stderr (spilling to an artifact if output exceeds the standard tool output budget) plus data with exit code, status (`completed` | `failed` | `timed_out` | `cancelled`), duration, and truncation flags. Always stages a `shell_executed` event with the full record.

## Configuration

Takes no plugin config; `create_plugin` ignores it. See [configuration](../../../../docs/configuration.md) for global config.

## Behavior and limits

- Executes via the invocation's process service in the workspace with a 1-hour timeout: login shell (`-lc`) on POSIX — `$SHELL`, then `bash`, `zsh`, `/bin/sh` — or PowerShell/`cmd` on Windows. The command string passes through verbatim; detection strips exactly one leading `!` (`!!foo` runs `!foo`).
- The outcome record never invents an exit code: cancelled/timed-out runs report what the process service actually returned.

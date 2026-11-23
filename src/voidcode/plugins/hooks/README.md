# hooks

User-configured argv commands gating tools and observing session lifecycle.

## Surface

No tools, commands, views, or queries. Registers `pre_tool` (gate), `post_tool` (note), `session_start`, and `session_end` callbacks only for surfaces that have commands configured. A callback is always registered with the tool-name wildcard `("*",)`; the plugin applies `pre_tool_match`/`post_tool_match` itself with case-sensitive stdlib `fnmatch` before running a command.

## Configuration

Plugin-only keys (strict: unknown keys are rejected):

- `enabled` (bool, default `true`): `false` disables every surface.
- `timeout_seconds` (default `30.0`, maximum `600`): per-command timeout.
- `pre_tool`, `post_tool`, `session_start`, `session_end`: lists of argv commands (`[[argv...], ...]`, each a non-empty array of non-empty strings).
- `pre_tool_match`, `post_tool_match`: tool-name glob filters (stdlib `fnmatch`, case-sensitive); empty matches every tool. A finite glob is evaluated by the plugin, so `write*` gates `write_file`.

See [configuration](../../../../docs/configuration.md) for global config. The product-injected `providers` key (product bootstrap hands every factory the resolved map) is accepted and ignored.

Hook commands are **trusted host-side code**, like plugins: they run unconfined with the core process environment and can do anything that process can. They are never sandboxed or claimed confined, are not watched by the sandbox supervisor, and declare no tool effects. Execution is argv-only: no shell, no command interpolation of tool data, and no approval prompt for the hook command itself.

## Hook command protocol

Every hook command runs with:

- `cwd` = the session workspace.
- `stdin` = `/dev/null`. No input is piped; read the payload from the environment.
- environment = the core process environment plus:
  - `VOIDCODE_RUNNING_TOOL_HOOK=1` — recursion guard; hooks are skipped in a core whose environment already has it set to `1`.
  - `VOIDCODE_HOOK_SURFACE` — `pre_tool`, `post_tool`, `session_start`, or `session_end`.
  - `VOIDCODE_SESSION_ID` — the session the callback belongs to.
  - `VOIDCODE_HOOK_PAYLOAD_JSON` — one bounded JSON object, described below.

Tool surfaces receive:

```json
{
  "phase": "pre",
  "tool_name": "write_file",
  "session_id": "session-...",
  "tool_call_id": "call-...",
  "arguments": {"path": "note.txt"},
  "arguments_truncated": false
}
```

A post-tool payload adds the settled outcome:

```json
{"status": "ok", "result": "bounded result prefix", "result_truncated": false}
```

Lifecycle surfaces receive the run payload as `VOIDCODE_HOOK_PAYLOAD_JSON`:
`{"phase": "start"|"end", "run_id": "...", "logical_turn": "..."}`, plus
`session_status` and, for a failed run, `error` on `end`.

Payload bounds: argument strings are cut at 1000 characters, each container keeps
32 items, nesting stops at depth 4, the arguments document stops at 8 KiB, the whole
payload at 16 KiB, and a post-tool result is only its first 4000 characters. The
`*_truncated` flags and `[truncated]` markers say when a bound applied; an oversized
arguments document is replaced by `{"_omitted": "..."}` so the environment value is
always valid JSON. Hook commands never receive arbitrary unbounded tool output.

### stdout, exit codes, and failures

- Exit `0` with empty stdout, or with non-JSON prose stdout, means "continue" and
  produces no note. A JSON-looking stdout that does not parse as
  `{"action": "continue"|"cancel", "diagnostic": string, "guidance": string}` is a
  failure. Unknown top-level keys are ignored; non-string `diagnostic`/`guidance`
  values are a failure.
- `action: "cancel"` denies a `pre_tool` call with the hook's `diagnostic` (or a
  default message), bounded to 2000 characters. On a post-tool surface `cancel` is
  advisory: the note carries its `guidance`, or its `diagnostic` when there is no
  guidance. Lifecycle surfaces keep both and continue the loop.
- A non-zero exit, spawn error, timeout, cancellation, or malformed response is a
  failure. `pre_tool` fails closed: the call is denied with a bounded reason that
  includes bounded stderr. `post_tool` and lifecycle surfaces fail open: the tool or
  run already decided, and the failure becomes a bounded visible note instead of
  being swallowed. Earlier guidance from the same surface is kept in that note.
- stderr is ignored on success and surfaced on failure, cut at 2000 characters.

All plugin notes and pre-tool denial reasons are bounded to 2000 characters.

### Timeouts and cancellation

The per-command timeout defaults to 30 seconds and is capped at 600. One surface's
whole command list also has a 300-second aggregate budget; a command started near
the deadline gets only the remaining time. Commands run in a new process session,
and a timeout or cancellation kills the whole process group (SIGKILL), so a
command's descendants do not outlive it. Captured stdout and stderr are each
capped at 256 KiB.

The executor polls a caller-provided abort signal every 50 ms and kills the process
group when it fires. Core hook leases currently construct their context without an
abort signal, so on today's production path cancellation is bounded only by the
timeouts above; a hook can delay a turn for at most its surface budget.

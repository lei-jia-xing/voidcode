# First-Task Success E2E Acceptance Flow

This document defines the canonical pre-MVP smoke path for VoidCode. It validates the default product path from environment readiness through a small real coding task.

## Acceptance Criteria

| # | Criterion | Command / Path |
|---|-----------|----------------|
| 1 | `voidcode doctor --workspace .` reports provider/model/auth/tooling readiness with a clear next step when something is missing | `src/voidcode/doctor/` |
| 2 | `voidcode run` can complete a read + small edit task through the runtime boundary | `src/voidcode/cli/app.py:_handle_run_command` |
| 3 | The flow covers approval/resume behavior when the task writes files or invokes risky operations | `src/voidcode/cli/app.py:_run_with_inline_approval` |
| 4 | `voidcode web --no-open` plus the Web client can exercise the same class of task | `src/voidcode/cli/app.py:web_command`, `src/voidcode/server.py:web` |
| 5 | Failure output is actionable: the user can tell what is missing and what command/config to try next | `src/voidcode/cli/app.py:_print_runtime_failure_footer` |
| 6 | The flow is documented as the canonical pre-MVP smoke path | This document |

## Step 1: Environment Readiness — `voidcode doctor`

```bash
voidcode doctor --workspace .
```

### Expected Output

The doctor checks provider/model/auth, local tools (ast-grep, formatters, LSP, MCP), and produces a `First task readiness` section:

```
VoidCode Capability Doctor [workspace=/path/to/workspace]
============================================================

Summary:
  Total checks: N
  [+] Ready: N
  [-] Missing: N        # shown only when > 0
  [!] Errors: N         # shown only when > 0
  [o] Not configured: N # shown only when > 0

First task readiness:
  status: ready | degraded | not_ready
  summary: <one-line human summary>
  next: <specific command or config change to try>
  provider: <provider name>
  model: <model identifier>
  auth_present: true | false
  blockers:                   # shown only when status != ready
    - <specific blocker>
  warnings:                   # shown only when status == degraded
    - <non-blocking warning>
```

### Error Handling

| `status` | What it means | What to do next |
|----------|---------------|-----------------|
| `not_ready` (config error) | `.voidcode.json` is invalid or missing | Fix the config file, then rerun `voidcode doctor` |
| `not_ready` (provider missing) | No provider/model configured | Run `voidcode config init --model provider/model` |
| `not_ready` (missing_auth) | API key not found in environment | Set the provider API key (e.g. `OPENAI_API_KEY`), then rerun doctor |
| `not_ready` (invalid_model) | Model name not recognized by the provider | Run `voidcode provider models <provider>` to see valid models |
| `degraded` | Provider is ready but local tools are missing | You can proceed with `voidcode run`, but address warnings for best results |
| `ready` | Everything is ready | Proceed to Step 2 |

For JSON consumption:

```bash
voidcode doctor --workspace . --json | jq '.first_task_readiness'
```

See also: [CapabilityDoctor](../src/voidcode/doctor/doctor.py), [reporter](../src/voidcode/doctor/reporter.py).

## Step 2: CLI Run — Read + Small Edit

```bash
# Read-only task (no approval needed)
voidcode run "read README.md" --workspace .

# Write task with approval gating
voidcode run "write hello.txt with contents 'Hello, VoidCode!'" --workspace . --approval-mode ask
```

### Expected Output (TTY mode)

```
EVENT tool_call: read_file
EVENT tool_result: read_file README.md
...
RESULT
<final text output>
```

When `--approval-mode ask` is set and a write tool is invoked:

```
▸ Tool call: write_file
  hello.txt
⚠ Approval required: write_file for hello.txt
Approve write_file for hello.txt? [y/N]:
```

### Error Handling

| Scenario | Exit Code | User Action |
|----------|-----------|-------------|
| Provider not configured | Non-zero (`EXIT_PROVIDER_ERROR`) | Run `voidcode doctor` first to diagnose |
| Approval denied | `EXIT_APPROVAL_DENIED` (see `src/voidcode/cli_support/__init__.py`) | Resume with `voidcode sessions resume <id> --approval-request-id <id> --approval-decision allow` |
| Runtime failure | `EXIT_RUNTIME_ERROR` | See failure footer in stderr for session id, debug command, and resume hint |
| Unknown command / disabled tool | `EXIT_INVALID_COMMAND` | Run `voidcode commands list` to see available tools |

The failure footer (printed to stderr) always includes:

```
VoidCode runtime failure summary
  session: <session-id>
  status: failed
  provider: <provider>          # when available
  model: <model>                # when available
  provider_error_kind: <kind>   # when available
  resumable: true | false
  last_successful_tool: <name>  # when available
  debug: voidcode sessions debug <session-id> --workspace .
  resume: voidcode sessions resume <session-id> --workspace .   # when resumable
```

See also: [`_handle_run_command`](../src/voidcode/cli/app.py), [`_run_with_inline_approval`](../src/voidcode/cli/app.py), [`_print_runtime_failure_footer`](../src/voidcode/cli/app.py).

## Step 3: Approval / Resume Flow

When a task is blocked waiting for approval or a question response:

```bash
# List sessions to find the waiting one
voidcode sessions list --workspace .

# Resume with approval decision
voidcode sessions resume <session-id> --workspace . \
  --approval-request-id <request-id> \
  --approval-decision allow

# Or answer a question
voidcode sessions answer <session-id> --workspace . \
  --question-request-id <request-id> \
  --response "your answer"
```

### Error Handling

| Scenario | Exit Code | User Action |
|----------|-----------|-------------|
| No pending approval/question | `EXIT_RUNTIME_ERROR` | Check `voidcode sessions debug <session-id>` for current state |
| Unknown session | `EXIT_INVALID_RESOURCE` | Verify session id with `voidcode sessions list` |
| Mismatched request id | `EXIT_INVALID_RESOURCE` | Get the correct request id from the debug snapshot |

See also: [`_pending_blocked_event`](../src/voidcode/cli/app.py), [`_print_trace_blocked`](../src/voidcode/cli/app.py).

## Step 4: Web Client Path

```bash
# Start the web client without auto-opening a browser
voidcode web --no-open --workspace . --port 8000

# In CI or headless environments, use the same command
voidcode web --no-open --workspace .
```

The web client exercises the same runtime boundary as the CLI:
- Session list, run, approval, question answer, replay, review tree/diff
- HTTP + SSE streaming at `http://127.0.0.1:8000`

### Error Handling

| Scenario | User Action |
|----------|-------------|
| Frontend bundle not found | Run `mise run frontend:build` first |
| Provider not configured | Same as CLI — run `voidcode doctor` first |
| Port already in use | Use `--port <different-port>` |

See also: [`web`](../src/voidcode/server.py), [`_resolve_frontend_dist`](../src/voidcode/server.py).

## Step 5: Debug and Recovery

```bash
# Get a full debug snapshot for a session
voidcode sessions debug <session-id> --workspace .

# Or via HTTP
curl http://127.0.0.1:8000/api/sessions/<session-id>/debug
```

The debug snapshot includes: session status, active/resumable/replayable flags, pending approval or question, last relevant event, last failure event, failure classification, last tool, provider context, and suggested operator action.

For comprehensive failure mode coverage, see the [Failure Diagnosis Runbook](./failure-diagnosis-runbook.md).

## Verification Checklist

To mark this flow as passing:

1. [ ] `mise run check` returns zero errors
2. [ ] `voidcode doctor --workspace .` reports `status: ready` or `status: degraded`
3. [ ] `voidcode run "read README.md" --workspace .` completes with output
4. [ ] `voidcode run "write hello.txt contents" --workspace . --approval-mode ask` triggers and resolves approval
5. [ ] `voidcode sessions list --workspace .` shows the completed session
6. [ ] `voidcode sessions resume <session-id> --workspace .` replays the full history
7. [ ] `voidcode web --no-open --workspace .` starts without errors
8. [ ] Failure footer on a failed run shows actionable next steps (session id, debug command, resume hint)

## Related Documentation

- [MVP Demo Guide](./mvp-demo-guide.md) — broader demo scenarios and evidence standards
- [Failure Diagnosis Runbook](./failure-diagnosis-runbook.md) — session state matrix and recovery actions
- [Current State](./current-state.md) — implementation status snapshot
- [Runtime Contracts](./contracts/README.md) — client-facing API contracts
- [Stream Transport](./contracts/stream-transport.md) — HTTP/SSE event streaming
- [Approval Flow](./contracts/approval-flow.md) — approval/resume contract details

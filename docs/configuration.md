# Configuration Guide

VoidCode supports configuration at the workspace level, user level, and per-invocation via CLI flags and environment variables.

## Configuration Sources

For `model` and `approval_mode`, settings resolve in priority order:
1. **CLI Flags & Request Overrides** (e.g. `--model`, `--approval-mode`)
2. **Workspace Configuration (`.voidcode.json`)**
3. **Environment Variables** (e.g. `VOIDCODE_MODEL`)
4. **User Configuration (`~/.config/voidcode/config.json` on POSIX)**
5. **System Defaults** (`yolo` approval mode)

Provider configurations merge repo-local over environment over user settings.
Inspect effective configuration at any time:

```bash
uv run voidcode config show --workspace .
```

To display the complete JSON Schema:

```bash
uv run voidcode config schema
```

## Initializing Workspace Configuration

Generate a starter `.voidcode.json` using the interactive or flag-driven CLI command:

```bash
# Generate config with a specific model
uv run voidcode config init --model openai/gpt-4o-mini --workspace .

# Include full example fields
uv run voidcode config init --model openai/gpt-4o-mini --with-examples --workspace .
```

## Configuration Format

A standard `.voidcode.json` file:

```json
{
  "$schema": "https://raw.githubusercontent.com/lei-jia-xing/voidcode/master/schema/voidcode.config.schema.json",
  "model": "openai/gpt-4o-mini",
  "approval_mode": "yolo"
}
```

### Key Settings

| Field | Type | Description |
|---|---|---|
| `$schema` | `string` | URL of the published schema for editor auto-completion. |
| `model` | `string` | Canonical provider and model name in `provider/model` format (e.g. `openai/gpt-4o-mini`). |
| `approval_mode` | `string` | Approval tier: `yolo` (default), `ask`, or `write`. |
| `tool_timeout_seconds` | `integer` | Optional timeout in seconds for individual tool executions (unset by default). |
| `reasoning_effort` | `string` | Optional reasoning effort for models supporting reasoning: `off`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max`. |
| `providers` | `object` | Custom provider endpoints and base URLs. |
| `tools` | `object` | Tool allowlists and per-tool configurations. |
| `skills` | `object` | Enabled skills and skill directory configurations. |
| `hooks` | `object` | Lifecycle hook configurations. |

## Approval Modes

VoidCode supports three approval tiers:

- **`yolo` (Default)**: Fully autonomous mode. Auto-approves tool executions without interactive prompts.
- **`ask`**: Prompts for user approval before executing state-modifying tools (file writes, edits, and shell commands). Read-only inspection tools (`read`, `glob`, `grep`, `ast_grep`) execute automatically.
- **`write`**: Automatically approves file write and edit operations, but prompts for approval on shell command execution (`shell_exec`).

In v1, approval modes govern tool permissions within the Python runtime; VoidCode does not provide an OS-level process sandbox.

Override the approval mode per invocation:

```bash
uv run voidcode run "Update dependency versions in package.json" --approval-mode write --workspace .
```

## Custom Provider Endpoints

To connect to private or proxy OpenAI-compatible endpoints:

```json
{
  "model": "my-provider/custom-model",
  "providers": {
    "custom": {
      "my-provider": {
        "base_url": "https://api.example.com/v1",
        "timeout_seconds": 30
      }
    }
  }
}
```

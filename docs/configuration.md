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

## Custom Primary Agents

VoidCode supports custom agent definitions written in Markdown with YAML frontmatter. Custom agents can operate as top-level primary agents or delegated subagents.

### Location

Custom agents are discovered in priority order (project overrides user):

1. **Project Agents**: `.voidcode/agents/<agent-id>.md`
2. **User Agents**: `~/.config/voidcode/agents/<agent-id>.md` (or `$XDG_CONFIG_HOME/voidcode/agents/`)

### Defining a Custom Primary Agent

To define a top-level primary agent, set `mode: primary` in the frontmatter:

```markdown
---
name: Lead Reviewer
id: lead-reviewer
description: Autonomous code review and quality assessment lead
mode: primary
model: openai/gpt-4o
tool_allowlist:
  - read
  - glob
  - grep
---
You are the lead reviewer for this workspace. Focus on correctness, edge cases, and maintainability.
```

### Selecting the Agent

Select the custom agent in `.voidcode.json`:

```json
{
  "agent": {
    "preset": "lead-reviewer"
  }
}
```

Or select it per-request via the `--agent` CLI option or request metadata:

```bash
uv run voidcode run "Review recent changes" --agent lead-reviewer --workspace .
```

During runtime execution and session resumption, custom primary agent identity, prompt materialization, and configuration are durably admitted through runtime composition without requiring installed Python packages.

## Prompt Commands & Command Lookup

Prompt commands (`/command [arguments]`) expand into user prompt text before core turn execution.

### Native slash actions

The interactive TUI and web composer reserve these runtime-owned actions; they are
not markdown prompt macros and do not invoke a provider turn unless stated:

#### Session commands

- **`/new [title]`** — creates a fresh empty session and switches the active
  view to it. Clears live transcript and composer state while preserving active
  workspace and model configuration. Rejects mid-task while a run is in flight.
- **`/clear`** — clears conversation history by transitioning to a new session
  with an empty transcript, retaining active runtime configuration. Rejects
  mid-task while a run is in flight.
- **`/resume [id | title]`** — switches to a saved session and reloads its
  persisted event history into the transcript. Bare `/resume` opens an
  interactive session picker; providing a session ID or title prefix resumes
  directly. Rejects mid-task while a run is in flight.
- **`/fork [title]`** — branches the current session from its historical event
  prefix up to the latest completed turn into a new independent session. Inherits
  parent capability snapshots (model, provider, reasoning effort) and accepts an
  optional title. Rejects mid-task while a run is in flight or during pending
  plan/review operations.
- **`/rename <title>`** — sets a user-facing title for the current session and
  records a `session.renamed` event. Bare `/rename` opens an in-place rename
  overlay; providing arguments renames immediately. Allowed concurrently even
  while a task is in progress.

#### Info commands

- **`/status`** — displays a structured diagnostic panel of current session
  state without invoking a provider turn. Reports session ID, execution status,
  active model and provider, base URL, collaboration mode, active thread goal
  (status and objective), and cumulative token/cost usage. Allowed mid-run.
- **`/export [path]`** — renders the full session event history into structured
  Markdown (`# Session`, `## User`, `## Assistant`, tool calls, and results).
  Bare `/export` writes to `voidcode-session-<id>.md` in the workspace root;
  passing a relative path writes to that location inside the workspace (path
  traversal outside the workspace is rejected). Because export is an explicit
  user-directed action, it writes directly without tool approval gates, but
  requires an idle session (rejected mid-task).
- **`/copy`** — extracts structured copy targets from the last assistant
  response (such as full text, fenced code blocks by language, or blockquotes).
  Bare `/copy` presents a choice selector or copies directly to the system
  clipboard via OSC 52 or local clipboard providers. Allowed mid-run.
- **`/context`** — inspects the token usage breakdown of the runtime's
  assembled prompt projection without advancing a model turn. Reports total used
  tokens, context window capacity, utilization percentage, and a segment-by-segment
  breakdown (System prompt, Agent prompt, Tools, Goal steering, and Conversation
  history). Allowed mid-run.

#### Collaboration & execution actions

- **`/compact`** — available for an idle active session. It asks the configured
  provider for a bounded continuation summary, persists the summary and compaction
  event, and reuses it after reopening the session. If a provider summary is
  unavailable, the runtime may still persist bounded tool-result compaction; the
  result reports that outcome explicitly. It fails visibly while the session is
  busy.
- **`/diff`** — reads the aggregate Git diff (staged, unstaged, and untracked
  files) without a model call. Clean workspaces, non-Git directories, and Git
  errors remain distinct states.
- **`/plan`** and **`/plan off`** — enter or leave the reversible collaboration
  mode. Plan mode narrows the shared runtime permission gate to read-only while
  preserving the session's immutable base policy; leaving it cannot relax an
  externally read-only session. No shell-command parsing is used.
- **`/review [focus | target]`** — runs the governed read-only review agent
  (`preset: review`) over one of four selectable review targets: uncommitted
  changes (`uncommitted`, default), base branch comparison (`base_branch`),
  specific commit (`commit`), or custom instructions (`custom`). Model selection
  prefers `review_model` (with automated reasoning effort fallback) and falls
  back to the parent session model. The review child requires a terminal `yield`
  invocation to return findings, preserving parent session history and state.
  Diffs exceeding 262,144 bytes (256 KiB) reject visibly rather than silently truncating.
- **`/goal [objective | clear | edit | pause | resume]`** — creates, inspects, or mutates
  the persistent thread goal for the active session (bare `/goal` prints status and usage).
  Long objectives (>4000 characters) are automatically spilled to
  `.voidcode/attachments/<uuid>/goal-objective.md`.
  When an active goal has a positive token budget, completed turns charge their
  token usage against the goal (plan mode turns excluded); reaching the budget
  transitions the status to `budget-limited`. `/goal clear` clears the goal,
  `/goal pause` suspends pursuit, `/goal resume` reactivates it with continuation
  guidance, and `/goal edit` loads the objective into the composer.
- **`!<command>`** — native shell escape. Runs the shell command directly in the
  workspace without starting a provider turn. Because typed `!` commands constitute
  explicit user action, they bypass approval checks and sandbox restrictions
  (including under read-only/plan mode). The command, duration, and output are
  persisted as a `runtime.user_shell_executed` event in session history, available
  idle or mid-turn for subsequent provider turn context.
- **`/btw <question>`** — native side question. Answers quick user inquiries
  directly against the current session context prefix without modifying session
  history, appending events, or altering transcripts. Runs exactly one provider turn
  with zero tools offered; works while the session is idle or running (no busy gate).
- **`/init`** — native contributor guide generation (Codex parity). Submits a fixed
  prompt instructing the model to inspect repository evidence and produce or
  maintain `AGENTS.md` without overwriting existing guidelines. Executes as a
  normal governed agent turn under session tool approval policies; disabled while
  a task is in progress (busy-gated).
Native commands are defined in a single authoritative table (`src/voidcode/command/native.py:NATIVE_COMMANDS`).
Inspection catalog generation (`GET /api/commands`), TUI validation/dispatch, and web client routing
derive from this shared definition. Native names are reserved: a custom markdown command with one
of these names is not used by native interactive dispatch. `GET /api/commands` reports implemented
native actions with `source: native`; `voidcode commands list` lists only the prompt-command
registry. Custom prompt commands continue to use their documented source precedence and `$ARGUMENTS`
expansion.
### Command Sources & Precedence

Commands load in priority order, with later sources overriding earlier definitions when names collide:

1. **Builtin commands** (lowest precedence; builtin slot in `loader.py`)
2. **Extension commands** (overrides builtin; loaded from installed component extensions and plugins)
3. **User command directory** (overrides extension and builtin; `~/.config/voidcode/commands/` or `$XDG_CONFIG_HOME/voidcode/commands/`)
4. **Project commands** (highest precedence; overrides user, extension, and builtin; `commands/**/*.md` and `.voidcode/commands/**/*.md`)

Prompt catalog listings (`voidcode commands list`) and prompt invocation share the identical authoritative prompt-command registry and resolve collisions identically. The HTTP `/api/commands` catalog additionally reports implemented native interactive actions with `source: native`; those reserved names are not markdown macro overrides.
### Defining Commands

Create a markdown file in `.voidcode/commands/<name>.md` or `~/.config/voidcode/commands/<name>.md`:

```markdown
---
description: Run repository checks and explain any failures
---
Run full verification for this repository. If any test or check fails, analyze the root cause and summarize next steps.
Focus area: $ARGUMENTS
```

### Inspecting Available Commands

List all registered commands and their winning sources:

```bash
uv run voidcode commands list --workspace .
```

Inspect a command template:

```bash
uv run voidcode commands show <name> --workspace .
```

## Hierarchical Project Instructions & Local Overrides

VoidCode supports hierarchical instructions aligned with Codex (`AGENTS.md` and `AGENTS.override.md`):

### Precedence per Directory

Within any directory, a local override file takes precedence over standard project instructions:
1. `AGENTS.override.md` (local uncommitted overrides; winning file)
2. `AGENTS.md` (standard committed project guidance)

### Project Root Hierarchy Discovery

When a session initializes with a workspace directory (serving as current working directory), the runtime resolves the project root by walking upward until the nearest `.git` directory or `.git` worktree file marker is found. If no `.git` marker exists, traversal stops strictly at the workspace boundary (preventing arbitrary parent filesystem inspection).

Instructions are loaded and ordered from project root down to the workspace directory:
- Project root instructions establish the baseline rules.
- Subproject/child directory instructions refine and specialize guidance.
- Root/ancestor baseline rules are selected before touched-file rules; overall file-count and byte limits still apply.
- An aggregate UTF-8 byte budget (`MAX_TOTAL_RULE_BYTES = 36,000`) includes truncation text; provider-visible rule content never exceeds the cap. If later applicable files are omitted at the cap, the final included rule's metadata reports `truncated: true`; zero-byte caps expose an empty truncated entry.

## Shell Command Approval

Shell commands use the active runtime approval mode (`ask`, `write`, or `yolo`) and explicit approve/deny decisions. Any configured `permission.rules[].command` value is a whole-command text glob matched against the original command string; it is not parsed for shell semantics or split into subcommands. Compound commands and quoting are passed unchanged to the shell, with approval providing the safety boundary.

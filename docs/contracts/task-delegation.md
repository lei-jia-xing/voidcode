# Task Delegation Contract

This document defines the interface and lifecycle contracts for background tasks and delegated child agent sessions in VoidCode.

## Overview

A top-level parent session delegates focused subtasks to child agent sessions via the built-in `task` tool (`src/voidcode/tools/delegation/task.py`). Delegated tasks run in background worker threads managed by `RuntimeBackgroundTaskSupervisor` (`src/voidcode/runtime/background/supervisor.py`).

Each delegated task creates a distinct child session row in SQLite persistence with `parent_session_id` set to the parent session's ID.

## Task Launch Schema

Launching a child session requires four mandatory properties:

| Property | Type | Description |
|---|---|---|
| `prompt` | `string` | The full prompt or instructions for the child session. |
| `run_in_background` | `boolean` | Set `true` to run asynchronously in a background worker and receive an immediate `task_id`. Set `false` for synchronous execution. |
| `load_skills` | `array[string]` | Skill names to force-load in the child context. Pass `[]` if no extra skills are needed. |
| `subagent_type` | `string` | The child preset ID defining the agent's role and tool permissions. |

### Verified Child Agent Presets

- **`explore`**: Fast workspace discovery, codebase navigation, and file search.
- **`review`**: Evidence review, diff inspection, and quality assessment.
- **`research`**: External documentation lookups and standards research.
- **`execute`**: Hands-on code modifications, refactoring, and test execution.
- **`plan`**: Read-only architectural planning and step formulation.

### Optional Launch Fields

- `description`: Optional short description of the delegated work.
- `keep_alive`: When `true` (with `run_in_background=true`), the child session remains alive after each turn, parking as idle (`awaiting_steer`) so the parent can send follow-up instructions via `task(operation="steer")`.
- `outputSchema`: Arbitrary JSON Schema for validating the child's final structured output. Requires `run_in_background=true`.
- `schemaMode`: Validation strictness (`"permissive"` or `"strict"`). Ignored without `outputSchema`.

### Example Launch Payload

```json
{
  "prompt": "Inspect the configuration loading path",
  "run_in_background": true,
  "load_skills": [],
  "subagent_type": "explore"
}
```

## Control Operations (`operation`)

The `task` tool supports four control operations using the `operation` property:

### 1. `output`
Retrieves structured output, summary text, or bounded transcripts from a completed or in-progress task.

Selector rules: Requires **exactly one** of `task_id`, `task_ids`, or `parallel_group_id`.

```json
{
  "operation": "output",
  "task_id": "task_12345"
}
```

Options: `block: true` waits for completion; `timeout: 30000` sets maximum wait milliseconds; `full_session: true` includes the child session transcript (allowed only with a single `task_id` selector).
### 2. `ps`
Inspects the status roster of background tasks belonging to the active parent session:

```json
{
  "operation": "ps"
}
```
Takes no arguments. Returns a list of task summaries (task ID, subagent type, status, created time) for the active parent session, capped at 100 entries.

### 3. `cancel`
Requests cancellation of an active or queued background task. Requires `task_id`.

```json
{
  "operation": "cancel",
  "task_id": "task_12345"
}
```

### 4. `steer`
Sends a new prompt turn to an idle or interrupted keep-alive child task. Requires `task_id` and `prompt`.

```json
{
  "operation": "steer",
  "task_id": "task_12345",
  "prompt": "Now inspect src/voidcode/runtime/execution/"
}
```
## Task Result Body

Tool invocations and task queries return a structured body containing correlation identifiers and completion metadata:
- `task_id`: The unique background task identifier.
- `parent_session_id`: ID of the initiating parent session.
- `session_id`: ID of the child session executing the work.
- `result_available`: Boolean indicating whether final output or structured data is ready to read.
- `status`: Current task status (`queued`, `running`, `idle`, `completed`, `failed`, `cancelled`, `interrupted`).

## Parent Lifecycle & Notification Events

The runtime notifies the parent session of background task state transitions via runtime events:

- `runtime.background_task_completed`: Emitted when a child task completes successfully.
- `runtime.background_task_failed`: Emitted when a child task encounters an unhandled failure.
- `runtime.background_task_waiting_approval`: Emitted when a child task is blocked waiting for human approval.
- `runtime.background_task_awaiting_steer`: Emitted when a keep-alive child task finishes a turn and pauses waiting for parent steering instructions.

## Operator CLI Commands

Operators inspect and control background tasks using the CLI:

```bash
voidcode tasks list --workspace .
voidcode tasks status <task-id>
voidcode tasks output <task-id>
voidcode tasks cancel <task-id>
voidcode tasks retry <task-id>
voidcode tasks steer <task-id> "Now inspect src/voidcode/runtime/execution/"
```

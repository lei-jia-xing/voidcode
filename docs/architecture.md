# VoidCode Architecture Summary

## Status and intent

VoidCode is a pre-MVP, local-first coding agent runtime inspired by OpenCode and Claude Code. The immediate goal is not to build a complete platform, but to make one developer task loop reliable from end to end:

1. a user submits a development task
2. the agent reasons, calls tools, requests approval when needed, and performs changes
3. the runtime records state and events
4. the user can observe progress and continue the session through a client such as the CLI

## System context

The system context can be described as a layered path from users to tools:

- users interact through a CLI client today, with room for a web frontend or future IDE client
- clients talk to the **VoidCode Runtime**
- the runtime coordinates sessions, permissions, hooks, tool registration, streaming, and storage
- the runtime invokes the **LangGraph orchestrator** for graph execution, state transitions, checkpoints, and interrupt/resume behavior
- LangGraph ultimately drives LLM providers, workspace access, and tool execution through the runtime boundary

Two boundaries are especially important:

- LangGraph does **not** talk directly to UI clients
- UI clients do **not** call tools directly

Everything flows through the runtime so governance, persistence, and observability stay consistent.

## LangGraph and custom runtime boundary

VoidCode uses LangGraph as the orchestration engine, not as the entire product runtime.

### LangGraph is responsible for

- the agent loop
- graph state
- conditional routing
- interrupt and resume
- checkpoints

### The custom runtime is responsible for

- tool registry and metadata
- permission decisions (`allow`, `deny`, `ask`)
- hook execution
- session creation, loading, and resume
- storage abstraction over SQLite
- streaming transport for CLI or future clients
- context management and compaction

This split is the core architectural decision for the project: **LangGraph orchestrates, while the runtime provides productized execution boundaries.**

## Key components

The codebase is expected to grow around three central areas:

### `runtime/`

Runtime services form the system center. This area will own session management, permission checks, hooks, transport, persistence, and the headless runtime entrypoint.

### `graph/`

Graph code models the main agent loop. The planned flow is roughly:

`prepare_context` → `call_model` → `decide_next_step` → `permission_gate` → `execute_tool` → `handle_tool_result` → `finalize_response`

The MVP intentionally keeps the graph small so the main loop becomes stable before the design expands.

### `tools/`

The tool layer will expose built-in capabilities such as `read`, `glob`, `grep`, `bash`, `write`, and `edit`, but only through the runtime pipeline:

graph tool request → runtime metadata lookup → permission check → before-hook → tool execution → after-hook → persistence → result back to graph

The design assumes read operations may run concurrently, while write operations stay controlled and approval-driven.

## Design principles

The current architecture is guided by a few explicit principles:

- **Clear layering:** keep UI, runtime, orchestration, and infrastructure separate.
- **Governance before execution:** every tool call passes through registry, permission, and hooks.
- **Recoverable state:** sessions, messages, approvals, tool executions, and checkpoints should be restorable.
- **Observable execution:** turns, tools, hooks, approvals, retries, and errors should emit events.
- **MVP first:** ship a stable single-agent loop before exploring multi-agent or plugin-heavy designs.

## MVP boundary

The MVP aims to include:

- a single-agent core loop
- a basic built-in tool set
- session persistence and resume
- approval and permission flow
- foundational hooks
- at least one usable entrypoint, such as the CLI
- a working headless runtime baseline

The MVP explicitly defers deeper IDE integration, cloud collaboration, plugin marketplaces, and advanced multi-agent coordination.

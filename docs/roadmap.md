# VoidCode Roadmap Summary

For the execution-focused checklist that turns this roadmap into concrete delivery phases, see [`docs/mvp-todo-plan.md`](./mvp-todo-plan.md). For client-facing runtime contracts, see [`docs/contracts/README.md`](./contracts/README.md).

## Current status

VoidCode is still in pre-MVP development. The roadmap is organized from foundation work through MVP integration. The repository has completed the initial environment/bootstrap work and now also includes one deterministic read-only CLI → runtime → graph → tool slice. It has also established the initial extension infrastructure for tools, skills, and carrier seams for LSP/ACP, while keeping the broader MVP implementation and IDE integrations out of scope for the current phase.

## MVP boundary

### In scope for MVP

- users can submit a development task
- the agent can read code, search code, and call tools
- write operations require approval
- sessions can be resumed
- the LangGraph main loop runs end to end
- basic hooks can fire
- event flow is observable

### Out of scope for MVP

- multi-agent team systems
- cloud collaboration
- IDE plugins
- plugin marketplace support
- advanced MCP ecosystem work
- complex visual workbenches

## Epic overview

### Epic 0: Foundation

Create the baseline repository and development environment: Python version policy, `uv`, `mise`, repository structure, and CI baseline.

**Current status:** substantially complete. The repo now has working developer setup, CI, contributor docs, and a first deterministic read-only runtime slice. Extension infrastructure has been established through a unified configuration schema, tool provider seams, and initial skill discovery.

### Epic 1: LangGraph Core Loop

Define graph state, nodes, graph compilation, and interrupt/resume so a single agent turn can execute.

### Epic 2: Runtime Skeleton

Build the custom runtime shell: session manager, runtime entrypoint, transport abstraction, and runtime-to-graph integration.

### Epic 3: Tool Registry and Extensions

Make tools and extensions first-class runtime capabilities with metadata, registration, built-ins, and a unified execution pipeline. This epic also includes the foundational infrastructure for skills, language servers (LSP), and the agent communication protocol (ACP) as runtime-managed seams.

### Epic 4: Permission Engine

Implement controlled execution through `allow`, `deny`, and `ask`, with approval required before writes and risky shell actions.

### Epic 5: Hook and Event Engine

Add event-driven extensibility through hook registration, pre/post-tool hooks, turn hooks, and hook execution logging. This includes the canonical runtime event schema for client observability and turn re-numbering.

### Epic 6: Storage and Recovery

Persist sessions and execution state in SQLite so interrupted work can be restored after restart.

### Epic 7: Context and Observability

Manage long-running context and provide trace-friendly visibility into turns, tools, approvals, hooks, and errors.

### Epic 8: CLI / Minimal UI

Expose the runtime through a usable entrypoint with streaming output, approval interaction, and session recovery.

### Epic 9: MVP Integration

Connect the full path into a demoable product loop with end-to-end testing, failure handling, demo scripts, and user documentation.

## Wave overview

- **Wave 1:** foundation, initial graph work, and runtime skeleton (**partially complete in repository form**)
- **Wave 2:** tool execution, permissions, and hooks
- **Wave 3:** storage, recovery, context, and observability
- **Wave 4:** entrypoints, integration, and MVP demo readiness

## MVP completion signal

The MVP boundary is considered met when VoidCode can reliably demonstrate one governed single-agent development task flow, with persistence, approvals, observability, and at least one usable client entrypoint.

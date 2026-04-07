# VoidCode Roadmap Summary

For the execution-focused checklist that turns this roadmap into concrete delivery phases, see [`docs/mvp-todo-plan.md`](./mvp-todo-plan.md). For client-facing runtime contracts, see [`docs/contracts/README.md`](./contracts/README.md).

## Current status

VoidCode is still in pre-MVP development. The roadmap is organized from foundation work through MVP integration. The repository has completed the initial environment/bootstrap work and now also includes a stable deterministic single-agent loop implementation. It has also established the initial extension infrastructure for tools, skills, and carrier seams for LSP/ACP, while keeping the broader MVP implementation and IDE integrations out of scope for the current phase.

## MVP boundary

### In scope for MVP

- users can submit a development task
- the agent can read code, search code, and call tools
- write operations require approval
- sessions can be resumed
- the deterministic single-agent loop runs end to end
- event flow is observable
- real-time runtime events are emitted for client rendering

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

**Current status:** substantially complete. The repo now has working developer setup, CI, contributor docs, and a stable deterministic single-agent loop. Extension infrastructure has been established through a unified configuration schema, tool provider seams, and initial skill discovery.

### Epic 1: LangGraph Core Loop

Define graph state, nodes, graph compilation, and interrupt/resume so a single agent turn can execute.

**Current status:** complete. The runtime now implements a stable deterministic single-agent loop with support for turn execution, tool resolution, and session resume.

### Epic 2: Runtime Skeleton

Build the custom runtime shell: session manager, runtime entrypoint, transport abstraction, and runtime-to-graph integration.

**Current status:** complete. The `VoidCodeRuntime` boundary is established, supporting both CLI and HTTP transports with unified session and event handling.

### Epic 3: Tool Registry and Extensions

Make tools and extensions first-class runtime capabilities with metadata, registration, built-ins, and a unified execution pipeline. This epic also includes the foundational infrastructure for skills, language servers (LSP), and the agent communication protocol (ACP) as runtime-managed seams.

**Current status:** partially complete. Built-in tools and skill discovery are implemented. LSP and ACP seams exist as configuration carriers and disabled stubs, with real integration deferred.

### Epic 4: Permission Engine

Implement controlled execution through `allow`, `deny`, and `ask`, with approval required before writes and risky shell actions.

**Current status:** complete. The runtime supports governed execution with approval-resume continuity across all transports.

### Epic 5: Hook and Event Engine

Add event-driven extensibility through hook registration, pre/post-tool hooks, turn hooks, and hook execution logging. This includes the canonical runtime event schema for client observability and turn re-numbering.

**Current status:** substantially complete. The runtime event schema is stable, and events are emitted for all major loop phases. Turn re-numbering is implemented to ensure sequence consistency across session resumes. Hook registration infrastructure exists in the configuration model, but real-time hook execution is still pending.

### Epic 6: Storage and Recovery

Persist sessions and execution state in SQLite so interrupted work can be restored after restart.

**Current status:** complete. Full session persistence including events, outputs, and pending approvals is implemented.

### Epic 7: Context and Observability

Manage long-running context and provide trace-friendly visibility into turns, tools, approvals, hooks, and errors.

**Current status:** partially complete. Turn-level observability through the event stream is implemented; longer-running context management is still evolving.

### Epic 8: TUI / CLI / Web Clients

Expose the runtime through usable entrypoints with streaming output, approval interaction, and session recovery.

**Current status:** in progress. The CLI supports basic task execution and approval. A TUI MVP is planned. The web client now has an initial live integration for session list, replay, and streamed runs, with broader client work still pending.


### Epic 9: MVP Integration

Connect the full path into a demoable product loop with end-to-end testing, failure handling, demo scripts, and user documentation.

## Wave overview

- **Wave 1:** foundation, initial graph work, and runtime skeleton (**partially complete in repository form**)
- **Wave 2:** tool execution, permissions, and hooks
- **Wave 3:** storage, recovery, context, and observability
- **Wave 4:** entrypoints, integration, and MVP demo readiness

## MVP completion signal

The MVP boundary is considered met when VoidCode can reliably demonstrate one governed single-agent development task flow, with persistence, approvals, observability, and at least one usable client entrypoint.

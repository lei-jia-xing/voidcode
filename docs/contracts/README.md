# Runtime Contracts

This directory contains the authoritative technical contracts for the VoidCode runtime, client integration, and execution model.

## Contracts

- [`runtime-execution.md`](./runtime-execution.md): Execution boundary, ownership leases, lifecycle, SQLite event persistence, and checkpoint recovery.
- [`task-delegation.md`](./task-delegation.md): Background task execution, child session presets, parent/child linkage, and result retrieval.
- [`client-transport.md`](./client-transport.md): CLI, HTTP/SSE streaming, ACP facade, and run completion semantics.

Detailed data shapes, configuration models, and tool definitions are defined directly in code and generated schemas:
- Configuration schema: `schema/voidcode.config.schema.json` (generated from `src/voidcode/runtime/config/models.py`).
- Tool definitions: `src/voidcode/tools/contracts.py`.
- Runtime events: `src/voidcode/runtime/events.py`.

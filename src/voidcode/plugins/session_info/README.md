# session_info

Read-only panels over the calling session: context makeup, usage, export, and copy.

## Surface

- Command `/context` (no arguments): what the model context is made of — measured input/cache tokens plus the latest dispatched request's message characters, tool-schema characters, tool count, and per-segment sizes. Errors if the transcript scan limit is hit before finding a dispatch.
- Command `/usage` (no arguments): measured turns, input/output/cache tokens, cost, elapsed seconds, unknown-usage flag, and the active budget when one is set.
- Command `/export` (no arguments): the session transcript as Markdown (messages, tool calls, tool results, run markers), capped at 200,000 characters.
- Command `/copy` (no arguments): copyable blocks from the last assistant response — fenced code blocks when present, else the full response text. Errors when there is no assistant response or the scan limit hides it.

Note: `/status` is core-only; this plugin exposes `context` / `usage` / `export` / `copy`, not `session_status`.

## Configuration

Takes no plugin config; `create_plugin` ignores it. See [configuration](../../../../docs/configuration.md) for global config.

## Behavior and limits

- Panels read the calling session's committed events (at most 2000, in pages of 200) and measured usage through SDK services only — never mutate state, never touch storage directly. (The core still persists a `COMMAND_COMPLETED` receipt for each panel invocation.)

Context presentations page through twenty segments using an optional `offset`.
They distinguish neutral-encoding character counts from cumulative session token
usage; context capacity and pressure remain unknown without measured metadata.
Usage fields preserve unknown and zero separately and label budget ceilings with
their owner and starting sequence. Reading panels does not mutate plugin state.
Factory config accepts only the injected `providers` map; other fields are errors.

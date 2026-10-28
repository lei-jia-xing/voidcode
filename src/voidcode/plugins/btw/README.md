# btw

Answer a quick side question from conversation context without interrupting the main turn.

## Surface

- Command `/btw` with required `question` (non-empty string). Runs with `busy_gate=False`, so it is available while the session is busy.
- Returns the side answer as output plus `{"question", "answer"}` data; empty questions, empty model responses, and model failures return errors.

## Configuration

Takes no plugin config; `create_plugin` ignores it. See [configuration](../../../../docs/configuration.md) for global config.

## Behavior and limits

- One provider turn via `services.model_query(..., include_context=True)`; declares only the `NETWORK` effect (no session or workspace mutation), so it stays available in read-only sessions.
- The question is wrapped with a tool-free, single-response constraint: the side turn has no tools and no follow-ups, and must say so when it does not know rather than offering to look it up.
- Stateless: keeps no state and stages no plugin events. (The core still records the model-query usage fact and the command's `COMMAND_COMPLETED` receipt.)
